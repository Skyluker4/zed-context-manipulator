"""Filtering and selection logic for threads, parts, and message positions.

Everything here is pure: it inspects :mod:`zed_context_manipulator.threadmodel`
and :mod:`zed_context_manipulator.database` objects and decides what matches.
The CLI and TUI build these query objects from user input.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .threadmodel import Part, Thread

if TYPE_CHECKING:
    from .database import ThreadRow


def parse_datetime(value: str | None) -> datetime | None:
    """Parse a user-supplied date/time into an aware UTC datetime."""

    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"Could not parse date/time: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_datetime(value: str | None) -> datetime | None:
    try:
        return parse_datetime(value)
    except ValueError:
        return None


def _compile(pattern: str | None, ignore_case: bool) -> re.Pattern[str] | None:
    if pattern is None:
        return None
    flags = re.IGNORECASE if ignore_case else 0
    return re.compile(pattern, flags)


def glob_matches(target: str, pattern: str) -> bool:
    """Pragmatic glob match against a path-like target."""

    if fnmatch.fnmatch(target, pattern):
        return True
    base = os.path.basename(target.rstrip("/"))
    if base and fnmatch.fnmatch(base, pattern):
        return True
    # Allow a bare pattern (no glob metacharacters) to match as a substring.
    if not any(ch in pattern for ch in "*?[]"):
        return pattern in target
    return False


@dataclass(slots=True)
class ThreadQuery:
    """Criteria for selecting whole threads."""

    thread_ids: set[str] | None = None
    title_contains: str | None = None
    title_regex: str | None = None
    content_contains: str | None = None
    content_regex: str | None = None
    folder_contains: str | None = None
    folder_glob: str | None = None
    project: str | None = None
    model_contains: str | None = None
    model_regex: str | None = None
    profile: str | None = None
    parent_id: str | None = None
    created_after: str | None = None
    created_before: str | None = None
    updated_after: str | None = None
    updated_before: str | None = None
    has_images: bool | None = None
    min_messages: int | None = None
    max_messages: int | None = None
    min_size: int | None = None
    max_size: int | None = None
    ignore_case: bool = True

    def needs_document(self) -> bool:
        """Whether matching requires decoding the thread payload."""

        return any(
            value is not None
            for value in (
                self.content_contains,
                self.content_regex,
                self.model_contains,
                self.model_regex,
                self.profile,
                self.has_images,
                self.min_messages,
                self.max_messages,
            )
        )

    def matches(self, row: ThreadRow, thread: Thread | None = None) -> bool:
        if self.thread_ids is not None and row.id not in self.thread_ids:
            return False
        if not self._match_title(row):
            return False
        if not self._match_folders(row):
            return False
        if self.parent_id is not None and (row.parent_id or "") != self.parent_id:
            return False
        if not self._match_dates(row):
            return False
        if not self._match_size(row):
            return False
        if self.needs_document():
            if thread is None:
                return False
            if not self._match_document(thread):
                return False
        return True

    def _match_title(self, row: ThreadRow) -> bool:
        title = row.summary or ""
        if self.title_contains is not None:
            haystack = title.lower() if self.ignore_case else title
            needle = self.title_contains.lower() if self.ignore_case else self.title_contains
            if needle not in haystack:
                return False
        regex = _compile(self.title_regex, self.ignore_case)
        if regex is not None and not regex.search(title):
            return False
        return True

    def _match_folders(self, row: ThreadRow) -> bool:
        folders = row.folders
        if self.folder_contains is not None:
            needle = self.folder_contains
            if self.ignore_case:
                if not any(needle.lower() in f.lower() for f in folders):
                    return False
            elif not any(needle in f for f in folders):
                return False
        if self.folder_glob is not None:
            if not any(glob_matches(f, self.folder_glob) for f in folders):
                return False
        if self.project is not None:
            target = self.project.lower() if self.ignore_case else self.project
            bases = [os.path.basename(f.rstrip("/")) for f in folders]
            if self.ignore_case:
                bases = [b.lower() for b in bases]
            if target not in bases:
                return False
        return True

    def _match_dates(self, row: ThreadRow) -> bool:
        created = _as_datetime(row.created_at)
        updated = _as_datetime(row.updated_at)
        bounds = (
            (self.created_after, created, "after"),
            (self.created_before, created, "before"),
            (self.updated_after, updated, "after"),
            (self.updated_before, updated, "before"),
        )
        for raw_bound, actual, direction in bounds:
            if raw_bound is None:
                continue
            bound = _as_datetime(raw_bound)
            if bound is None or actual is None:
                if raw_bound is not None and actual is None:
                    return False
                continue
            if direction == "after" and actual < bound:
                return False
            if direction == "before" and actual > bound:
                return False
        return True

    def _match_size(self, row: ThreadRow) -> bool:
        size = row.raw_size
        if self.min_size is not None and size < self.min_size:
            return False
        if self.max_size is not None and size > self.max_size:
            return False
        return True

    def _match_document(self, thread: Thread) -> bool:
        if self.content_contains is not None or self.content_regex is not None:
            haystack = f"{thread.title}\n{thread.text()}"
            if self.content_contains is not None:
                hay = haystack.lower() if self.ignore_case else haystack
                needle = (
                    self.content_contains.lower() if self.ignore_case else self.content_contains
                )
                if needle not in hay:
                    return False
            regex = _compile(self.content_regex, self.ignore_case)
            if regex is not None and not regex.search(haystack):
                return False
        if self.model_contains is not None or self.model_regex is not None:
            provider, model = thread.next_model
            label = f"{provider or ''}/{model or ''}"
            if self.model_contains is not None:
                hay = label.lower() if self.ignore_case else label
                needle = self.model_contains.lower() if self.ignore_case else self.model_contains
                if needle not in hay:
                    return False
            regex = _compile(self.model_regex, self.ignore_case)
            if regex is not None and not regex.search(label):
                return False
        if self.profile is not None and (thread.profile or "") != self.profile:
            return False
        if self.has_images is not None:
            found = any(part.has_image for part in thread.iter_parts())
            if found != self.has_images:
                return False
        count = len(thread.messages)
        if self.min_messages is not None and count < self.min_messages:
            return False
        if self.max_messages is not None and count > self.max_messages:
            return False
        return True


@dataclass(slots=True)
class PartQuery:
    """Criteria for selecting individual parts within a thread."""

    roles: set[str] | None = None
    kinds: set[str] | None = None
    tool_names: set[str] | None = None
    path_globs: list[str] = field(default_factory=list)
    path_regex: str | None = None
    target_regex: str | None = None
    content_contains: str | None = None
    content_regex: str | None = None
    is_error: bool | None = None
    images_only: bool = False
    min_size: int | None = None
    max_size: int | None = None
    message_indices: set[int] | None = None
    ignore_case: bool = True

    @property
    def is_empty(self) -> bool:
        return all(
            value in (None, False, [], set())
            for value in (
                self.roles,
                self.kinds,
                self.tool_names,
                self.path_globs,
                self.path_regex,
                self.target_regex,
                self.content_contains,
                self.content_regex,
                self.is_error,
                self.images_only,
                self.min_size,
                self.max_size,
                self.message_indices,
            )
        )

    def matches(self, part: Part) -> bool:
        if self.message_indices is not None and part.message_index not in self.message_indices:
            return False
        if self.roles is not None and part.role not in self.roles:
            return False
        if self.kinds is not None and part.kind not in self.kinds:
            return False
        if self.images_only and not part.has_image:
            return False
        if self.tool_names is not None:
            name = (part.tool_name or "").lower() if self.ignore_case else (part.tool_name or "")
            wanted = {t.lower() for t in self.tool_names} if self.ignore_case else self.tool_names
            if name not in wanted:
                return False
        if self.is_error is not None and bool(part.is_error) != self.is_error:
            return False
        if not self._match_target(part):
            return False
        if not self._match_content(part):
            return False
        if not self._match_size(part):
            return False
        return True

    def _match_target(self, part: Part) -> bool:
        if self.path_globs:
            target = part.target
            if not target:
                return False
            if not any(glob_matches(target, pattern) for pattern in self.path_globs):
                return False
        regex = _compile(self.path_regex, self.ignore_case)
        if regex is not None:
            if not part.target or not regex.search(part.target):
                return False
        treg = _compile(self.target_regex, self.ignore_case)
        if treg is not None:
            if not part.target or not treg.search(part.target):
                return False
        return True

    def _match_content(self, part: Part) -> bool:
        if self.content_contains is None and self.content_regex is None:
            return True
        text = part.text()
        if self.content_contains is not None:
            hay = text.lower() if self.ignore_case else text
            needle = self.content_contains.lower() if self.ignore_case else self.content_contains
            if needle not in hay:
                return False
        regex = _compile(self.content_regex, self.ignore_case)
        if regex is not None and not regex.search(text):
            return False
        return True

    def _match_size(self, part: Part) -> bool:
        if self.min_size is None and self.max_size is None:
            return True
        size = part.size()
        if self.min_size is not None and size < self.min_size:
            return False
        if self.max_size is not None and size > self.max_size:
            return False
        return True


@dataclass(slots=True)
class PositionSelector:
    """Select messages by their position within a thread."""

    oldest: int | None = None
    newest: int | None = None
    index_min: int | None = None
    index_max: int | None = None
    keep_oldest: int | None = None
    keep_newest: int | None = None
    middle: bool = False

    @property
    def is_empty(self) -> bool:
        return (
            self.oldest is None
            and self.newest is None
            and self.index_min is None
            and self.index_max is None
            and not self.middle
        )

    def select(self, message_count: int) -> set[int]:
        """Return the set of message indices selected for the given count."""

        if self.is_empty:
            return set(range(message_count))
        selected: set[int] = set()
        if self.oldest is not None:
            selected |= set(range(0, min(self.oldest, message_count)))
        if self.newest is not None:
            selected |= set(range(max(0, message_count - self.newest), message_count))
        if self.index_min is not None or self.index_max is not None:
            low = self.index_min if self.index_min is not None else 0
            high = self.index_max if self.index_max is not None else message_count - 1
            selected |= {i for i in range(message_count) if low <= i <= high}
        if self.middle:
            skip_low = self.keep_oldest or 0
            skip_high = self.keep_newest or 0
            selected |= set(range(skip_low, max(skip_low, message_count - skip_high)))
        return selected
