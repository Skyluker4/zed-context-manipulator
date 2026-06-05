"""Low-level access to the Zed agent ``threads.db`` SQLite database.

This module knows how to locate the database, read and write the raw
``threads`` rows, decode/encode the compressed thread payloads, and create
timestamped backups before any destructive operation.

It deliberately contains *no* knowledge of the thread document schema; that
lives in :mod:`zed_context_manipulator.threadmodel`.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import zstandard as zstd
except ImportError as exc:  # pragma: no cover - dependency is declared in pyproject
    raise SystemExit(
        "The 'zstandard' package is required. Install it with 'pip install zstandard' "
        "or 'pip install zed-context-manipulator'."
    ) from exc


DEFAULT_RELATIVE_DB = "zed/threads/threads.db"


def default_db_path() -> Path:
    """Return the default location of Zed's ``threads.db``.

    Honours ``$XDG_DATA_HOME`` and falls back to ``~/.local/share``.
    """

    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / DEFAULT_RELATIVE_DB


def resolve_db_path(raw: str | os.PathLike[str] | None) -> Path:
    """Expand ``~`` and environment variables in a user-supplied path."""

    if raw is None:
        return default_db_path()
    text = os.path.expanduser(os.path.expandvars(str(raw)))
    return Path(text)


@dataclass(slots=True)
class ThreadRow:
    """A single row of the ``threads`` table, with its payload decoded lazily."""

    id: str
    summary: str
    updated_at: str | None
    created_at: str | None
    data_type: str
    parent_id: str | None
    folder_paths: str | None
    folder_paths_order: str | None
    _data: bytes
    _doc: dict[str, Any] | None = None

    @property
    def folders(self) -> list[str]:
        """Project folder paths associated with this thread."""

        if not self.folder_paths:
            return []
        return [line for line in self.folder_paths.splitlines() if line.strip()]

    def doc(self) -> dict[str, Any]:
        """Return the decoded thread document, decoding on first access."""

        if self._doc is None:
            self._doc = decode_payload(self.data_type, self._data)
        return self._doc

    @property
    def raw_size(self) -> int:
        """Size in bytes of the stored (possibly compressed) payload."""

        return len(self._data)


class ThreadDatabase:
    """A thin wrapper around the ``threads`` SQLite table."""

    def __init__(self, path: Path | str, *, read_only: bool = False) -> None:
        self.path = Path(path)
        path = self.path
        self.read_only = read_only
        if not path.exists():
            raise FileNotFoundError(f"Zed threads database not found at: {path}")
        if read_only:
            uri = f"file:{path}?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True)
        else:
            self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self._columns = self._discover_columns()

    def _discover_columns(self) -> set[str]:
        rows = self.conn.execute("PRAGMA table_info(threads)").fetchall()
        return {row["name"] for row in rows}

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> ThreadDatabase:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _select_columns(self) -> str:
        wanted = [
            "id",
            "summary",
            "updated_at",
            "created_at",
            "data_type",
            "parent_id",
            "folder_paths",
            "folder_paths_order",
            "data",
        ]
        present = [c for c in wanted if c in self._columns]
        return ", ".join(present)

    def _row_to_thread(self, row: sqlite3.Row) -> ThreadRow:
        keys = row.keys()

        def get(name: str) -> Any:
            return row[name] if name in keys else None

        return ThreadRow(
            id=row["id"],
            summary=get("summary") or "",
            updated_at=get("updated_at"),
            created_at=get("created_at"),
            data_type=get("data_type") or "json",
            parent_id=get("parent_id"),
            folder_paths=get("folder_paths"),
            folder_paths_order=get("folder_paths_order"),
            _data=bytes(get("data") or b""),
        )

    def iter_threads(self) -> Iterator[ThreadRow]:
        """Yield every thread row, ordered by update time (newest last)."""

        order = "updated_at" if "updated_at" in self._columns else "id"
        query = f"SELECT {self._select_columns()} FROM threads ORDER BY {order} ASC"
        for row in self.conn.execute(query):
            yield self._row_to_thread(row)

    def load_threads(self) -> list[ThreadRow]:
        return list(self.iter_threads())

    def get_thread(self, thread_id: str) -> ThreadRow | None:
        query = f"SELECT {self._select_columns()} FROM threads WHERE id = ?"
        row = self.conn.execute(query, (thread_id,)).fetchone()
        return self._row_to_thread(row) if row else None

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0])

    def update_thread_document(
        self,
        thread_id: str,
        doc: dict[str, Any],
        *,
        data_type: str = "zstd",
        touch: bool = False,
    ) -> None:
        """Persist a modified thread document back to the database."""

        self._ensure_writable()
        payload = encode_payload(data_type, doc)
        sets = ["data_type = ?", "data = ?"]
        params: list[Any] = [data_type, payload]
        if touch and "updated_at" in self._columns:
            stamp = _now_iso()
            sets.append("updated_at = ?")
            params.append(stamp)
            doc["updated_at"] = stamp
        params.append(thread_id)
        self.conn.execute(
            f"UPDATE threads SET {', '.join(sets)} WHERE id = ?",
            params,
        )

    def update_metadata(self, thread_id: str, **columns: Any) -> None:
        """Update plain columns (summary, folder_paths, ...) for a thread."""

        self._ensure_writable()
        usable = {k: v for k, v in columns.items() if k in self._columns}
        if not usable:
            return
        assignments = ", ".join(f"{key} = ?" for key in usable)
        params = list(usable.values())
        params.append(thread_id)
        self.conn.execute(
            f"UPDATE threads SET {assignments} WHERE id = ?",
            params,
        )

    def delete_thread(self, thread_id: str) -> None:
        self._ensure_writable()
        self.conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))

    def commit(self) -> None:
        self._ensure_writable()
        self.conn.commit()

    def vacuum(self) -> None:
        self._ensure_writable()
        self.conn.execute("VACUUM")
        self.conn.commit()

    def has_column(self, name: str) -> bool:
        return name in self._columns

    def _ensure_writable(self) -> None:
        if self.read_only:
            raise RuntimeError("Database opened read-only; cannot modify.")


def decode_payload(data_type: str, data: bytes) -> dict[str, Any]:
    """Decode a stored thread payload into a Python dict."""

    if data_type == "zstd":
        decompressor = zstd.ZstdDecompressor()
        try:
            raw = decompressor.decompress(data)
        except zstd.ZstdError:
            with decompressor.stream_reader(data) as reader:
                raw = reader.read()
        return json.loads(raw.decode("utf-8"))
    if data_type == "json":
        return json.loads(data.decode("utf-8"))
    raise ValueError(f"Unknown thread data_type: {data_type!r}")


def encode_payload(data_type: str, doc: dict[str, Any]) -> bytes:
    """Encode a thread document back into the stored representation."""

    raw = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if data_type == "zstd":
        return zstd.ZstdCompressor(level=3).compress(raw)
    if data_type == "json":
        return raw
    raise ValueError(f"Unknown thread data_type: {data_type!r}")


def backup_database(path: Path, *, suffix: str | None = None) -> Path:
    """Copy the database next to itself with a timestamped ``.bak`` suffix."""

    stamp = suffix or datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak.{stamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
