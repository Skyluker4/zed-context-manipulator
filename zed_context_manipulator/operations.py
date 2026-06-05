"""High-level operations: turn queries plus an action into concrete changes.

This module is the bridge between the pure model/filter layers and the
database. It produces a :class:`RunReport` describing every change, which the
CLI prints (and the TUI summarises) regardless of whether the run is a dry run
or an actual write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .database import ThreadDatabase, ThreadRow, backup_database
from .filters import PartQuery, PositionSelector, ThreadQuery
from .threadmodel import PLACEHOLDER, Part, Thread

ACTION_PLACEHOLDER = "placeholder"
ACTION_REMOVE = "remove"
ACTION_STRIP_IMAGES = "strip-images"
ACTION_REPLACE = "replace"
ACTION_SET_TEXT = "set-text"

ACTION_CHOICES = (
    ACTION_PLACEHOLDER,
    ACTION_REMOVE,
    ACTION_STRIP_IMAGES,
    ACTION_REPLACE,
    ACTION_SET_TEXT,
)


@dataclass(slots=True)
class PartAction:
    """Describes what to do with each selected part."""

    mode: str
    placeholder: str = PLACEHOLDER
    replace_pattern: str | None = None
    replace_with: str = ""
    set_text_value: str | None = None
    ignore_case: bool = True

    def _compiled(self) -> re.Pattern[str] | None:
        if self.replace_pattern is None:
            return None
        flags = re.IGNORECASE if self.ignore_case else 0
        return re.compile(self.replace_pattern, flags)

    def apply(self, part: Part) -> bool:
        """Apply the action to ``part``; return ``True`` if it changed."""

        if self.mode == ACTION_REMOVE:
            part.dropped = True
            return True
        if self.mode == ACTION_PLACEHOLDER:
            part.to_placeholder(self.placeholder)
            return True
        if self.mode == ACTION_STRIP_IMAGES:
            return part.strip_images(self.placeholder)
        if self.mode == ACTION_REPLACE:
            regex = self._compiled()
            if regex is None or not part.editable:
                return False
            old = part.text()
            new = regex.sub(self.replace_with, old)
            if new != old:
                part.set_text(new)
                return True
            return False
        if self.mode == ACTION_SET_TEXT:
            if not part.editable or self.set_text_value is None:
                return False
            part.set_text(self.set_text_value)
            return True
        raise ValueError(f"Unknown action mode: {self.mode!r}")


@dataclass(slots=True)
class ThreadOp:
    """Thread-level (whole-conversation) operations."""

    delete: bool = False
    folder: str | None = None
    set_model: tuple[str, str] | None = None
    set_profile: str | None = None
    set_title: str | None = None

    @property
    def is_empty(self) -> bool:
        return (
            not self.delete
            and self.folder is None
            and self.set_model is None
            and self.set_profile is None
            and self.set_title is None
        )


@dataclass(slots=True)
class PartChange:
    pid: str
    message_index: int
    role: str
    kind: str
    tool_name: str | None
    target: str | None
    action: str
    preview: str


@dataclass(slots=True)
class ThreadReport:
    thread_id: str
    title: str
    folders: list[str]
    changes: list[PartChange] = field(default_factory=list)
    thread_actions: list[str] = field(default_factory=list)
    doc_dirty: bool = False
    meta_updates: dict[str, object] = field(default_factory=dict)
    delete: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.changes) or self.doc_dirty or bool(self.meta_updates) or self.delete


@dataclass(slots=True)
class RunReport:
    threads: list[ThreadReport] = field(default_factory=list)
    write: bool = False
    backup_path: Path | None = None
    vacuumed: bool = False
    scanned: int = 0

    @property
    def changed_threads(self) -> list[ThreadReport]:
        return [t for t in self.threads if t.changed]

    @property
    def total_part_changes(self) -> int:
        return sum(len(t.changes) for t in self.threads)

    @property
    def deleted_threads(self) -> int:
        return sum(1 for t in self.threads if t.delete)


def select_parts(
    thread: Thread,
    position: PositionSelector,
    part_query: PartQuery,
    *,
    invert: bool = False,
    keep_latest_images: int | None = None,
) -> list[Part]:
    """Return the parts in ``thread`` that an action should target."""

    message_indices = position.select(len(thread.messages))
    candidates: list[Part] = []
    for part in thread.iter_parts():
        if part.message_index not in message_indices:
            continue
        matched = True if part_query.is_empty else part_query.matches(part)
        if invert:
            matched = not matched
        if matched:
            candidates.append(part)
    if keep_latest_images:
        image_parts = [p for p in candidates if p.has_image]
        keep_ids = {id(p) for p in image_parts[-keep_latest_images:]}
        candidates = [p for p in candidates if not (p.has_image and id(p) in keep_ids)]
    return candidates


def matching_threads(
    rows: list[ThreadRow],
    query: ThreadQuery,
) -> list[tuple[ThreadRow, Thread | None]]:
    """Filter rows by a thread query, decoding documents only when needed."""

    results: list[tuple[ThreadRow, Thread | None]] = []
    needs_doc = query.needs_document()
    for row in rows:
        thread: Thread | None = None
        if needs_doc:
            thread = Thread(row.doc())
        if query.matches(row, thread):
            results.append((row, thread))
    return results


def _apply_thread_op(
    row: ThreadRow,
    thread: Thread,
    op: ThreadOp,
    report: ThreadReport,
) -> None:
    if op.delete:
        report.delete = True
        report.thread_actions.append("delete thread")
        return
    if op.set_model is not None:
        provider, model = op.set_model
        thread.set_next_model(provider, model)
        report.doc_dirty = True
        report.thread_actions.append(f"set next model -> {provider}/{model}")
    if op.set_profile is not None:
        thread.set_profile(op.set_profile)
        report.doc_dirty = True
        report.thread_actions.append(f"set profile -> {op.set_profile}")
    if op.set_title is not None:
        thread.set_title(op.set_title)
        report.doc_dirty = True
        report.meta_updates["summary"] = op.set_title
        report.thread_actions.append(f"set title -> {op.set_title}")
    if op.folder is not None:
        folders = [line for line in op.folder.splitlines() if line.strip()]
        joined = "\n".join(folders)
        report.meta_updates["folder_paths"] = joined or None
        report.meta_updates["folder_paths_order"] = joined or None
        report.thread_actions.append(f"reassign folder -> {joined or '(none)'}")


def process_thread(
    row: ThreadRow,
    thread: Thread,
    *,
    position: PositionSelector,
    part_query: PartQuery,
    action: PartAction | None,
    invert: bool = False,
    keep_latest_images: int | None = None,
    thread_op: ThreadOp | None = None,
) -> ThreadReport:
    """Compute (in memory) all changes for a single thread."""

    report = ThreadReport(
        thread_id=row.id,
        title=row.summary or thread.title,
        folders=row.folders,
    )
    if thread_op is not None and not thread_op.is_empty:
        _apply_thread_op(row, thread, thread_op, report)
        if report.delete:
            return report
    if action is not None:
        candidates = select_parts(
            thread,
            position,
            part_query,
            invert=invert,
            keep_latest_images=keep_latest_images,
        )
        for part in candidates:
            preview = part.preview(80)
            tool_name = part.tool_name
            target = part.target
            kind = part.kind
            if action.apply(part):
                report.changes.append(
                    PartChange(
                        pid=part.pid,
                        message_index=part.message_index,
                        role=part.role,
                        kind=kind,
                        tool_name=tool_name,
                        target=target,
                        action=action.mode,
                        preview=preview,
                    )
                )
        if report.changes:
            thread.apply()
            report.doc_dirty = True
    return report


def execute(
    db: ThreadDatabase,
    rows: list[ThreadRow],
    *,
    thread_query: ThreadQuery,
    position: PositionSelector,
    part_query: PartQuery,
    action: PartAction | None,
    invert: bool = False,
    keep_latest_images: int | None = None,
    thread_op: ThreadOp | None = None,
    write: bool = False,
    backup: bool = True,
    vacuum: bool = False,
    touch: bool = False,
    limit: int | None = None,
) -> RunReport:
    """Run an operation across all matching threads and optionally persist it."""

    report = RunReport(write=write)
    selected = matching_threads(rows, thread_query)
    report.scanned = len(selected)
    processed = 0
    for row, maybe_thread in selected:
        if limit is not None and processed >= limit:
            break
        thread = maybe_thread if maybe_thread is not None else Thread(row.doc())
        thread_report = process_thread(
            row,
            thread,
            position=position,
            part_query=part_query,
            action=action,
            invert=invert,
            keep_latest_images=keep_latest_images,
            thread_op=thread_op,
        )
        report.threads.append(thread_report)
        if thread_report.changed:
            processed += 1

    if write and report.changed_threads:
        if backup:
            report.backup_path = backup_database(db.path)
        for thread_report in report.changed_threads:
            if thread_report.delete:
                db.delete_thread(thread_report.thread_id)
                continue
            if thread_report.doc_dirty:
                row = _find_row(rows, thread_report.thread_id)
                if row is not None:
                    db.update_thread_document(
                        thread_report.thread_id,
                        row.doc(),
                        data_type=row.data_type if row.data_type in ("zstd", "json") else "zstd",
                        touch=touch,
                    )
            if thread_report.meta_updates:
                db.update_metadata(thread_report.thread_id, **thread_report.meta_updates)
        db.commit()
        if vacuum:
            db.vacuum()
            report.vacuumed = True
    return report


def _find_row(rows: list[ThreadRow], thread_id: str) -> ThreadRow | None:
    for row in rows:
        if row.id == thread_id:
            return row
    return None
