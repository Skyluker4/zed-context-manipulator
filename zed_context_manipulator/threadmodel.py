"""A normalized, format-agnostic view over Zed thread documents.

Zed has shipped (at least) two on-disk message encodings:

* **new** -- a flat object with ``role`` plus ``segments`` / ``tool_uses`` /
  ``tool_results`` lists.
* **old** -- a serde enum, ``{"User": {...}}`` or ``{"Agent": {...}}``, whose
  ``content`` list holds single-key variant objects (``Text``, ``Thinking``,
  ``Image``, ``Mention``, ``ToolUse``) and whose ``tool_results`` is a map
  keyed by tool-use id.

:class:`Thread`, :class:`Message`, and :class:`Part` present a single shape to
the rest of the program while keeping references into the live document so that
edits and removals can be written back without losing unknown fields.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Part kinds
# ---------------------------------------------------------------------------

KIND_TEXT = "text"
KIND_THINKING = "thinking"
KIND_IMAGE = "image"
KIND_TOOL_USE = "tool_use"
KIND_TOOL_RESULT = "tool_result"
KIND_MENTION = "mention"
KIND_OTHER = "other"

ALL_KINDS = (
    KIND_TEXT,
    KIND_THINKING,
    KIND_IMAGE,
    KIND_TOOL_USE,
    KIND_TOOL_RESULT,
    KIND_MENTION,
    KIND_OTHER,
)

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

PLACEHOLDER = (
    "[Content omitted from Zed thread history by zed-context-manipulator to "
    "reduce request payload.]"
)
IMAGE_PLACEHOLDER = (
    "[Image omitted from Zed thread history by zed-context-manipulator to reduce "
    "request payload. Re-run the tool if the image is needed again.]"
)

# Ordered lookups used to pull a human-meaningful "target" out of a tool call.
_TARGET_KEYS: tuple[tuple[str, str], ...] = (
    ("path", "path"),
    ("file_path", "path"),
    ("source_path", "path"),
    ("destination_path", "path"),
    ("glob", "glob"),
    ("include_pattern", "glob"),
    ("regex", "regex"),
    ("pattern", "regex"),
    ("command", "command"),
    ("url", "url"),
    ("uri", "url"),
    ("urls", "url"),
    ("query", "query"),
    ("q", "query"),
)


def is_image_object(obj: Any) -> bool:
    """Return ``True`` if ``obj`` looks like a serialized image content part."""

    if not isinstance(obj, dict):
        return False
    lowered = {str(k).lower() for k in obj}
    if "image" in lowered:
        return True
    return "source" in lowered and "text" not in lowered


def _content_parts(content: Any) -> list[Any]:
    if isinstance(content, list):
        return content
    if content is None:
        return []
    return [content]


def content_has_image(content: Any) -> bool:
    return any(is_image_object(part) for part in _content_parts(content))


def content_to_text(content: Any) -> str:
    """Flatten a tool-result ``content`` value into searchable text."""

    chunks: list[str] = []
    for part in _content_parts(content):
        if isinstance(part, str):
            chunks.append(part)
        elif isinstance(part, dict):
            if "Text" in part and isinstance(part["Text"], str):
                chunks.append(part["Text"])
            elif "text" in part and isinstance(part["text"], str):
                chunks.append(part["text"])
            elif is_image_object(part):
                chunks.append("[image]")
    return "\n".join(chunks)


def text_content_part() -> dict[str, str]:
    return {"Text": ""}


@dataclass(slots=True)
class Part:
    """A single addressable unit inside a message."""

    kind: str
    slot: str  # "segment" | "content" | "tool_use" | "tool_result"
    index: int  # original position within its slot
    raw: Any  # live reference into the document
    message_index: int
    role: str
    variant: str | None = None  # old-format content variant key, e.g. "Text"
    tool_name: str | None = None
    tool_use_id: str | None = None
    is_error: bool | None = None
    target: str | None = None  # extracted path/glob/url/command/query
    target_kind: str | None = None
    dropped: bool = False

    # -- identity -----------------------------------------------------------
    @property
    def pid(self) -> str:
        return f"{self.message_index}:{self.slot}:{self.index}"

    # -- introspection ------------------------------------------------------
    @property
    def has_image(self) -> bool:
        if self.kind == KIND_IMAGE:
            return True
        if self.kind == KIND_TOOL_RESULT:
            return content_has_image(self._result_content())
        return False

    def _result_content(self) -> Any:
        if isinstance(self.raw, dict):
            return self.raw.get("content")
        return None

    def text(self) -> str:
        """Return searchable text for this part (never ``None``)."""

        kind = self.kind
        raw = self.raw
        if kind == KIND_TEXT:
            if self.variant == "Text":
                return raw.get("Text", "") if isinstance(raw, dict) else str(raw)
            return raw.get("text", "") if isinstance(raw, dict) else str(raw)
        if kind == KIND_THINKING:
            if self.variant == "Thinking" and isinstance(raw, dict):
                inner = raw.get("Thinking")
                if isinstance(inner, dict):
                    return inner.get("text", "")
            return raw.get("text", "") if isinstance(raw, dict) else ""
        if kind == KIND_MENTION and isinstance(raw, dict):
            inner = raw.get("Mention")
            if isinstance(inner, dict):
                return str(inner.get("content", ""))
        if kind == KIND_TOOL_USE:
            return f"{self.tool_name or ''} {json.dumps(self._tool_input())}"
        if kind == KIND_TOOL_RESULT:
            return content_to_text(self._result_content())
        if kind == KIND_IMAGE:
            return "[image]"
        return json.dumps(raw, ensure_ascii=False) if not isinstance(raw, str) else raw

    def _tool_input(self) -> Any:
        if not isinstance(self.raw, dict):
            return {}
        if "input" in self.raw:
            return self.raw["input"]
        raw_input = self.raw.get("raw_input")
        if isinstance(raw_input, str):
            try:
                return json.loads(raw_input)
            except json.JSONDecodeError:
                return raw_input
        return raw_input or {}

    def preview(self, width: int = 100) -> str:
        snippet = " ".join(self.text().split())
        if len(snippet) > width:
            return snippet[: width - 1] + "\u2026"
        return snippet

    def size(self) -> int:
        """Approximate serialized size of this part, in bytes."""

        try:
            return len(json.dumps(self.raw, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            return len(str(self.raw).encode("utf-8"))

    # -- editing ------------------------------------------------------------
    @property
    def editable(self) -> bool:
        return self.kind in (KIND_TEXT, KIND_THINKING, KIND_MENTION, KIND_TOOL_RESULT)

    def set_text(self, value: str) -> None:
        """Replace the textual payload of this part in place."""

        kind = self.kind
        raw = self.raw
        if kind == KIND_TEXT:
            if self.variant == "Text" and isinstance(raw, dict):
                raw["Text"] = value
            elif isinstance(raw, dict):
                raw["text"] = value
            return
        if kind == KIND_THINKING and isinstance(raw, dict):
            if self.variant == "Thinking" and isinstance(raw.get("Thinking"), dict):
                raw["Thinking"]["text"] = value
            else:
                raw["text"] = value
            return
        if kind == KIND_MENTION and isinstance(raw, dict):
            inner = raw.get("Mention")
            if isinstance(inner, dict):
                inner["content"] = value
            return
        if kind == KIND_TOOL_RESULT and isinstance(raw, dict):
            raw["content"] = {"Text": value}
            if "output" in raw:
                raw["output"] = None
            return

    def to_placeholder(self, placeholder: str = PLACEHOLDER) -> None:
        """Collapse this part into a lightweight text placeholder in place."""

        kind = self.kind
        raw = self.raw
        if kind == KIND_TOOL_RESULT and isinstance(raw, dict):
            raw["content"] = {"Text": placeholder}
            if "output" in raw:
                raw["output"] = None
            return
        if kind == KIND_IMAGE:
            # Convert the image variant into a text variant in place.
            if isinstance(raw, dict):
                raw.clear()
                raw["Text"] = placeholder
                self.variant = "Text"
                self.kind = KIND_TEXT
            return
        self.set_text(placeholder)

    def strip_images(self, placeholder: str = IMAGE_PLACEHOLDER) -> bool:
        """Replace any image data with a placeholder, keeping other content.

        Returns ``True`` if anything was changed.
        """

        if self.kind == KIND_IMAGE:
            self.to_placeholder(placeholder)
            return True
        if self.kind == KIND_TOOL_RESULT and isinstance(self.raw, dict):
            content = self.raw.get("content")
            parts = _content_parts(content)
            changed = False
            new_parts: list[Any] = []
            for part in parts:
                if is_image_object(part):
                    new_parts.append({"Text": placeholder})
                    changed = True
                else:
                    new_parts.append(part)
            if changed:
                self.raw["content"] = new_parts
                if "output" in self.raw:
                    self.raw["output"] = None
            return changed
        return False


@dataclass(slots=True)
class Message:
    """A single message, normalized across encodings."""

    index: int
    role: str
    fmt: str  # "new" | "old"
    raw: dict[str, Any]
    body: dict[str, Any]  # for old format the inner User/Agent dict; else == raw
    parts: list[Part] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(p.text() for p in self.parts if not p.dropped)

    def iter_parts(self, *, include_dropped: bool = False) -> Iterator[Part]:
        for part in self.parts:
            if part.dropped and not include_dropped:
                continue
            yield part

    @property
    def is_empty(self) -> bool:
        return all(p.dropped for p in self.parts)

    def apply(self) -> None:
        """Rewrite the underlying document containers from ``parts``."""

        kept = [p for p in self.parts if not p.dropped]
        if self.fmt == "new":
            self.raw["segments"] = [p.raw for p in kept if p.slot == "segment"]
            self.raw["tool_uses"] = [p.raw for p in kept if p.slot == "tool_use"]
            self.raw["tool_results"] = [p.raw for p in kept if p.slot == "tool_result"]
        else:
            self.body["content"] = [p.raw for p in kept if p.slot == "content"]
            result_parts = [p for p in kept if p.slot == "tool_result"]
            if "tool_results" in self.body or result_parts:
                rebuilt: dict[str, Any] = {}
                for part in result_parts:
                    key = part.tool_use_id or f"result-{part.index}"
                    rebuilt[key] = part.raw
                self.body["tool_results"] = rebuilt


def _detect_role(message: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Return ``(role, fmt, body)`` for a raw message object."""

    if "role" in message:
        role = str(message.get("role", "")).lower() or ROLE_USER
        return role, "new", message
    if "User" in message and isinstance(message["User"], dict):
        return ROLE_USER, "old", message["User"]
    if "user" in message and isinstance(message["user"], dict):
        return ROLE_USER, "old", message["user"]
    if "Agent" in message and isinstance(message["Agent"], dict):
        return ROLE_ASSISTANT, "old", message["Agent"]
    if "agent" in message and isinstance(message["agent"], dict):
        return ROLE_ASSISTANT, "old", message["agent"]
    return ROLE_USER, "new", message


def _segment_kind(segment: Any) -> str:
    if not isinstance(segment, dict):
        return KIND_OTHER
    seg_type = str(segment.get("type", "")).lower()
    if seg_type == "text":
        return KIND_TEXT
    if "thinking" in seg_type:
        return KIND_THINKING
    return KIND_OTHER


def _old_variant_kind(variant: str) -> str:
    mapping = {
        "Text": KIND_TEXT,
        "Thinking": KIND_THINKING,
        "RedactedThinking": KIND_THINKING,
        "Image": KIND_IMAGE,
        "Mention": KIND_MENTION,
        "ToolUse": KIND_TOOL_USE,
    }
    return mapping.get(variant, KIND_OTHER)


def extract_tool_target(name: str | None, tool_input: Any) -> tuple[str | None, str | None]:
    """Pull a representative target string out of a tool's input."""

    if isinstance(tool_input, str):
        return tool_input, "query"
    if not isinstance(tool_input, dict):
        return None, None
    for key, label in _TARGET_KEYS:
        if key in tool_input:
            value = tool_input[key]
            if isinstance(value, list):
                value = " ".join(str(v) for v in value)
            if value is not None and str(value) != "":
                return str(value), label
    # Fall back to the first non-empty string value.
    for value in tool_input.values():
        if isinstance(value, str) and value:
            return value, None
    return None, None


class Thread:
    """A parsed thread document plus convenience accessors."""

    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = doc
        self.messages: list[Message] = []
        self._parse()

    # -- parsing ------------------------------------------------------------
    def _parse(self) -> None:
        raw_messages = self.doc.get("messages")
        if not isinstance(raw_messages, list):
            return
        for idx, raw in enumerate(raw_messages):
            if not isinstance(raw, dict):
                continue
            role, fmt, body = _detect_role(raw)
            message = Message(index=idx, role=role, fmt=fmt, raw=raw, body=body)
            if fmt == "new":
                self._parse_new(message, body)
            else:
                self._parse_old(message, body)
            self.messages.append(message)

    def _parse_new(self, message: Message, body: dict[str, Any]) -> None:
        uses_by_id: dict[str, str] = {}
        targets_by_id: dict[str, tuple[str | None, str | None]] = {}
        for i, seg in enumerate(body.get("segments", []) or []):
            message.parts.append(
                Part(
                    kind=_segment_kind(seg),
                    slot="segment",
                    index=i,
                    raw=seg,
                    message_index=message.index,
                    role=message.role,
                )
            )
        for i, tool_use in enumerate(body.get("tool_uses", []) or []):
            name = tool_use.get("name") if isinstance(tool_use, dict) else None
            tu_id = tool_use.get("id") if isinstance(tool_use, dict) else None
            tool_input = tool_use.get("input") if isinstance(tool_use, dict) else None
            target, target_kind = extract_tool_target(name, tool_input)
            if tu_id:
                uses_by_id[tu_id] = name or ""
                targets_by_id[tu_id] = (target, target_kind)
            message.parts.append(
                Part(
                    kind=KIND_TOOL_USE,
                    slot="tool_use",
                    index=i,
                    raw=tool_use,
                    message_index=message.index,
                    role=message.role,
                    tool_name=name,
                    tool_use_id=tu_id,
                    target=target,
                    target_kind=target_kind,
                )
            )
        for i, result in enumerate(body.get("tool_results", []) or []):
            tu_id = result.get("tool_use_id") if isinstance(result, dict) else None
            name = uses_by_id.get(tu_id or "")
            target, target_kind = targets_by_id.get(tu_id or "", (None, None))
            message.parts.append(
                Part(
                    kind=KIND_TOOL_RESULT,
                    slot="tool_result",
                    index=i,
                    raw=result,
                    message_index=message.index,
                    role=message.role,
                    tool_name=name,
                    tool_use_id=tu_id,
                    is_error=bool(result.get("is_error")) if isinstance(result, dict) else None,
                    target=target,
                    target_kind=target_kind,
                )
            )

    def _parse_old(self, message: Message, body: dict[str, Any]) -> None:
        targets_by_id: dict[str, tuple[str | None, str | None]] = {}
        names_by_id: dict[str, str] = {}
        for i, part in enumerate(body.get("content", []) or []):
            if not isinstance(part, dict) or not part:
                continue
            variant = next(iter(part.keys()))
            kind = _old_variant_kind(variant)
            tool_name = tu_id = target = target_kind = None
            if kind == KIND_TOOL_USE:
                inner = part.get(variant)
                if isinstance(inner, dict):
                    tool_name = inner.get("name")
                    tu_id = inner.get("id")
                    tool_input = inner.get("input")
                    if tool_input is None and "raw_input" in inner:
                        try:
                            tool_input = json.loads(inner["raw_input"])
                        except (json.JSONDecodeError, TypeError):
                            tool_input = inner.get("raw_input")
                    target, target_kind = extract_tool_target(tool_name, tool_input)
                    if tu_id:
                        names_by_id[tu_id] = tool_name or ""
                        targets_by_id[tu_id] = (target, target_kind)
            message.parts.append(
                Part(
                    kind=kind,
                    slot="content",
                    index=i,
                    raw=part,
                    message_index=message.index,
                    role=message.role,
                    variant=variant,
                    tool_name=tool_name,
                    tool_use_id=tu_id,
                    target=target,
                    target_kind=target_kind,
                )
            )
        results = body.get("tool_results")
        if isinstance(results, dict):
            items = list(results.items())
        elif isinstance(results, list):
            items = [(str(i), r) for i, r in enumerate(results)]
        else:
            items = []
        for i, (key, result) in enumerate(items):
            if not isinstance(result, dict):
                continue
            tu_id = result.get("tool_use_id") or key
            name = result.get("tool_name") or names_by_id.get(tu_id or "")
            target, target_kind = targets_by_id.get(tu_id or "", (None, None))
            message.parts.append(
                Part(
                    kind=KIND_TOOL_RESULT,
                    slot="tool_result",
                    index=i,
                    raw=result,
                    message_index=message.index,
                    role=message.role,
                    tool_name=name,
                    tool_use_id=tu_id,
                    is_error=bool(result.get("is_error")),
                    target=target,
                    target_kind=target_kind,
                )
            )

    # -- aggregate views ----------------------------------------------------
    def iter_parts(self, *, include_dropped: bool = False) -> Iterator[Part]:
        for message in self.messages:
            yield from message.iter_parts(include_dropped=include_dropped)

    def text(self) -> str:
        return "\n".join(m.text() for m in self.messages)

    @property
    def part_count(self) -> int:
        return sum(1 for _ in self.iter_parts())

    # -- thread metadata ----------------------------------------------------
    @property
    def title(self) -> str:
        for key in ("title", "summary"):
            value = self.doc.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    def set_title(self, value: str) -> None:
        self.doc["title"] = value
        self.doc["summary"] = value

    @property
    def next_model(self) -> tuple[str | None, str | None]:
        model = self.doc.get("model")
        if isinstance(model, dict):
            return model.get("provider"), model.get("model")
        return None, None

    def set_next_model(self, provider: str, model: str) -> None:
        self.doc["model"] = {"provider": provider, "model": model}

    @property
    def profile(self) -> str | None:
        value = self.doc.get("profile")
        return value if isinstance(value, str) else None

    def set_profile(self, value: str) -> None:
        self.doc["profile"] = value

    # -- writing back -------------------------------------------------------
    def apply(self) -> None:
        for message in self.messages:
            message.apply()
