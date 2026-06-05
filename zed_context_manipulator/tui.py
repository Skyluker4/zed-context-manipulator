"""A curses-based terminal UI for browsing and editing Zed threads.

The UI has two main screens:

* **thread list** -- search/filter threads, mark them for deletion, reassign
  their project folder, change the next model, or open one.
* **thread detail** -- browse every message/part, select parts, and stage drop
  or edit actions.

All edits are staged in memory and only written (with an automatic backup) when
you press ``w``.
"""

from __future__ import annotations

import curses
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .database import ThreadDatabase, ThreadRow, backup_database
from .filters import PartQuery, ThreadQuery
from .operations import (
    ACTION_PLACEHOLDER,
    ACTION_REMOVE,
    ACTION_SET_TEXT,
    ACTION_STRIP_IMAGES,
    PartAction,
    ThreadOp,
)
from .threadmodel import Thread

STATE_LIST = "list"
STATE_DETAIL = "detail"

HELP_LIST = (
    "enter open  / search  s sort  d del  r folder  m model  t title  w write  q quit  ? help"
)
HELP_DETAIL = (
    "space sel  d drop  D remove  i img  e edit  u undo  / find  s sort  a all  w write  q back"
)


@dataclass
class ThreadStage:
    """Pending, uncommitted changes for one thread."""

    part_actions: dict[str, PartAction] = field(default_factory=dict)
    op: ThreadOp = field(default_factory=ThreadOp)

    @property
    def empty(self) -> bool:
        return not self.part_actions and self.op.is_empty


@dataclass
class DetailRow:
    """A single rendered line in the detail view."""

    kind: str  # "header" | "part"
    text: str
    message_index: int
    part = None  # set for kind == "part"


class App:
    """The curses application controller."""

    def __init__(self, stdscr, db: ThreadDatabase, query: ThreadQuery, *, read_only: bool):
        self.stdscr = stdscr
        self.db = db
        self.base_query = query
        self.read_only = read_only
        self.rows: list[ThreadRow] = []
        self.view: list[ThreadRow] = []
        self.thread_cache: dict[str, Thread] = {}
        self.stage: dict[str, ThreadStage] = {}

        self.state = STATE_LIST
        self.list_index = 0
        self.list_offset = 0
        self.list_search = ""
        self.list_sort = "updated"

        self.current_thread: ThreadRow | None = None
        self.detail_rows: list[DetailRow] = []
        self.detail_index = 0
        self.detail_offset = 0
        self.detail_search = ""
        self.detail_sort = "document"
        self.selected: set[str] = set()

        self.message = "Loading..."
        self._init_colors()
        self.reload()

    # -- data ---------------------------------------------------------------
    def _init_colors(self) -> None:
        self.use_color = False
        try:
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_CYAN, -1)
                curses.init_pair(2, curses.COLOR_YELLOW, -1)
                curses.init_pair(3, curses.COLOR_RED, -1)
                curses.init_pair(4, curses.COLOR_GREEN, -1)
                self.use_color = True
        except curses.error:
            self.use_color = False

    def color(self, pair: int) -> int:
        return curses.color_pair(pair) if self.use_color else 0

    def reload(self) -> None:
        self.rows = self.db.load_threads()
        if self.base_query is not None:
            from .operations import matching_threads

            self.rows = [row for row, _ in matching_threads(self.rows, self.base_query)]
        self.rows.reverse()  # newest first
        self.apply_list_search()
        self.message = f"{len(self.view)} thread(s)."

    def get_thread(self, row: ThreadRow) -> Thread:
        if row.id not in self.thread_cache:
            self.thread_cache[row.id] = Thread(row.doc())
        return self.thread_cache[row.id]

    def stage_for(self, thread_id: str) -> ThreadStage:
        return self.stage.setdefault(thread_id, ThreadStage())

    def apply_list_search(self) -> None:
        needle = self.list_search.strip().lower()
        if not needle:
            self.view = list(self.rows)
        else:
            view = []
            for row in self.rows:
                if needle in (row.summary or "").lower() or any(
                    needle in f.lower() for f in row.folders
                ):
                    view.append(row)
                else:
                    try:
                        if needle in self.get_thread(row).text().lower():
                            view.append(row)
                    except (ValueError, KeyError):
                        continue
            self.view = view
        self._sort_view()
        self.list_index = min(self.list_index, max(0, len(self.view) - 1))
        self.list_offset = 0

    def _sort_view(self) -> None:
        if self.list_sort == "size":
            self.view.sort(key=lambda r: r.raw_size, reverse=True)
        elif self.list_sort == "title":
            self.view.sort(key=lambda r: (r.summary or "").lower())
        else:
            self.view.sort(key=lambda r: r.updated_at or "", reverse=True)

    def cycle_list_sort(self) -> None:
        order = ["updated", "size", "title"]
        self.list_sort = order[(order.index(self.list_sort) + 1) % len(order)]
        self._sort_view()
        self.message = f"Sorted by {self.list_sort}."

    # -- rendering ----------------------------------------------------------
    def safe_addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = self.stdscr.getmaxyx()
        if y < 0 or y >= height or x >= width:
            return
        clipped = text[: max(0, width - x - 1)]
        try:
            self.stdscr.addstr(y, x, clipped, attr)
        except curses.error:
            pass

    def pending_count(self) -> int:
        return sum(
            len(s.part_actions) + (0 if s.op.is_empty else 1)
            for s in self.stage.values()
            if not s.empty
        )

    def draw(self) -> None:
        self.stdscr.erase()
        if self.state == STATE_LIST:
            self.draw_list()
        else:
            self.draw_detail()
        self.draw_status()
        self.stdscr.refresh()

    def draw_status(self) -> None:
        height, width = self.stdscr.getmaxyx()
        pending = self.pending_count()
        ro = " [READ-ONLY]" if self.read_only else ""
        left = f" {self.message}"
        right = f"pending: {pending}{ro} "
        bar = left + " " * max(0, width - len(left) - len(right) - 1) + right
        self.safe_addstr(height - 2, 0, bar[: width - 1], curses.A_REVERSE)
        hint = HELP_LIST if self.state == STATE_LIST else HELP_DETAIL
        self.safe_addstr(height - 1, 0, hint, self.color(1))

    def draw_list(self) -> None:
        height, width = self.stdscr.getmaxyx()
        title = "Zed Context Manipulator"
        sub = f"  threads: {len(self.view)}/{len(self.rows)}   sort: {self.list_sort}"
        if self.list_search:
            sub += f"   filter: '{self.list_search}'"
        self.safe_addstr(0, 0, (title + sub).ljust(width - 1), curses.A_BOLD)
        body = height - 3
        if self.list_index < self.list_offset:
            self.list_offset = self.list_index
        elif self.list_index >= self.list_offset + body:
            self.list_offset = self.list_index - body + 1
        for i in range(body):
            idx = self.list_offset + i
            if idx >= len(self.view):
                break
            row = self.view[idx]
            self._draw_list_row(1 + i, idx, row, width)

    def _draw_list_row(self, y: int, idx: int, row: ThreadRow, width: int) -> None:
        stage = self.stage.get(row.id)
        marker = " "
        attr = 0
        if stage and not stage.empty:
            if stage.op.delete:
                marker, attr = "X", self.color(3)
            else:
                marker, attr = "*", self.color(2)
        date = (row.updated_at or "")[:10]
        title = row.summary or "(untitled)"
        project = os.path.basename((row.folders[0].rstrip("/")) if row.folders else "")
        line = f"{marker} {date} {_short_size(row.raw_size):>6}  {title}"
        if project:
            line += f"   [{project}]"
        row_attr = attr
        if idx == self.list_index:
            row_attr |= curses.A_REVERSE
        self.safe_addstr(y, 0, line.ljust(width - 1), row_attr)

    def build_detail_rows(self, thread: Thread) -> None:
        needle = self.detail_search.strip().lower()
        part_query = PartQuery(content_contains=self.detail_search) if needle else None
        if self.detail_sort in ("size", "length"):
            self._build_detail_rows_sorted(thread, part_query)
            return
        rows: list[DetailRow] = []
        for message in thread.messages:
            header = DetailRow(
                kind="header",
                text=f"[{message.index}] {message.role.upper()} ({message.fmt})",
                message_index=message.index,
            )
            part_lines: list[DetailRow] = []
            for part in message.iter_parts():
                if part_query is not None and not part_query.matches(part):
                    continue
                dr = DetailRow(kind="part", text="", message_index=message.index)
                dr.part = part
                part_lines.append(dr)
            if part_query is not None and not part_lines:
                continue
            rows.append(header)
            rows.extend(part_lines)
        self.detail_rows = rows
        self.detail_index = min(self.detail_index, max(0, len(rows) - 1))

    def _build_detail_rows_sorted(self, thread: Thread, part_query) -> None:
        parts: list[DetailRow] = []
        for message in thread.messages:
            for part in message.iter_parts():
                if part_query is not None and not part_query.matches(part):
                    continue
                dr = DetailRow(kind="part", text="", message_index=message.index)
                dr.part = part
                parts.append(dr)
        if self.detail_sort == "length":
            parts.sort(key=lambda d: d.part.length(), reverse=True)
        else:
            parts.sort(key=lambda d: d.part.size(), reverse=True)
        self.detail_rows = parts
        self.detail_index = min(self.detail_index, max(0, len(parts) - 1))

    def draw_detail(self) -> None:
        height, width = self.stdscr.getmaxyx()
        row = self.current_thread
        if row is None:
            return
        title = row.summary or "(untitled)"
        head = f"{title}"
        if self.detail_sort != "document":
            head += f"   sort: {self.detail_sort}"
        if self.detail_search:
            head += f"   find: '{self.detail_search}'"
        self.safe_addstr(0, 0, head.ljust(width - 1), curses.A_BOLD)
        body = height - 3
        if self.detail_index < self.detail_offset:
            self.detail_offset = self.detail_index
        elif self.detail_index >= self.detail_offset + body:
            self.detail_offset = self.detail_index - body + 1
        stage = self.stage.get(row.id)
        for i in range(body):
            idx = self.detail_offset + i
            if idx >= len(self.detail_rows):
                break
            self._draw_detail_row(1 + i, idx, self.detail_rows[idx], stage, width)

    def _draw_detail_row(self, y, idx, dr: DetailRow, stage, width) -> None:
        if dr.kind == "header":
            attr = self.color(1) | curses.A_BOLD
            if idx == self.detail_index:
                attr |= curses.A_REVERSE
            self.safe_addstr(y, 0, dr.text.ljust(width - 1), attr)
            return
        part = dr.part
        pid = part.pid
        action = stage.part_actions.get(pid) if stage else None
        mark = "[x]" if pid in self.selected else "[ ]"
        flag = " "
        attr = 0
        if action is not None:
            attr = self.color(2)
            flag = {
                ACTION_PLACEHOLDER: "D",
                ACTION_REMOVE: "R",
                ACTION_STRIP_IMAGES: "I",
                ACTION_SET_TEXT: "E",
            }.get(action.mode, "?")
        tag = part.kind + (f":{part.tool_name}" if part.tool_name else "")
        target = f" {part.target}" if part.target else ""
        img = "img " if part.has_image else ""
        if self.detail_sort == "length" and not part.has_image:
            metric = f"{part.length()}c"
        else:
            metric = _short_size(part.size())
        text = f"  {mark}{flag} {tag} {img}{metric}{target}  {part.preview(width)}"
        if idx == self.detail_index:
            attr |= curses.A_REVERSE
        self.safe_addstr(y, 0, text[: width - 1], attr)

    # -- prompts ------------------------------------------------------------
    def prompt(self, label: str, initial: str = "") -> str | None:
        height, width = self.stdscr.getmaxyx()
        curses.curs_set(1)
        buffer = list(initial)
        try:
            while True:
                shown = "".join(buffer)
                self.safe_addstr(height - 2, 0, (label + shown).ljust(width - 1), curses.A_REVERSE)
                self.stdscr.move(height - 2, min(len(label) + len(shown), width - 1))
                self.stdscr.refresh()
                key = self.stdscr.getch()
                if key in (curses.KEY_ENTER, 10, 13):
                    return "".join(buffer)
                if key == 27:  # Esc
                    return None
                if key in (curses.KEY_BACKSPACE, 127, 8):
                    if buffer:
                        buffer.pop()
                elif 32 <= key <= 126:
                    buffer.append(chr(key))
        finally:
            curses.curs_set(0)

    def confirm(self, label: str) -> bool:
        answer = self.prompt(f"{label} (y/N): ")
        return bool(answer) and answer.strip().lower() in ("y", "yes")

    def show_pager(self, text: str, title: str) -> None:
        height, width = self.stdscr.getmaxyx()
        lines: list[str] = []
        for raw_line in text.splitlines() or [""]:
            while len(raw_line) > width - 1:
                lines.append(raw_line[: width - 1])
                raw_line = raw_line[width - 1 :]
            lines.append(raw_line)
        top = 0
        body = height - 2
        while True:
            self.stdscr.erase()
            self.safe_addstr(0, 0, title.ljust(width - 1), curses.A_BOLD)
            for i in range(body):
                if top + i >= len(lines):
                    break
                self.safe_addstr(1 + i, 0, lines[top + i])
            self.safe_addstr(height - 1, 0, "j/k scroll  q close", self.color(1))
            self.stdscr.refresh()
            key = self.stdscr.getch()
            if key in (ord("q"), 27):
                break
            if key in (ord("j"), curses.KEY_DOWN) and top + body < len(lines):
                top += 1
            elif key in (ord("k"), curses.KEY_UP) and top > 0:
                top -= 1
            elif key == curses.KEY_NPAGE:
                top = min(top + body, max(0, len(lines) - 1))
            elif key == curses.KEY_PPAGE:
                top = max(top - body, 0)

    def edit_in_editor(self, initial: str) -> str | None:
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
        with tempfile.NamedTemporaryFile(
            "w+", suffix=".txt", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(initial)
            tmp = handle.name
        try:
            curses.endwin()
            subprocess.call([editor, tmp])
        except OSError:
            return None
        finally:
            self.stdscr.refresh()
            curses.doupdate()
        try:
            with open(tmp, encoding="utf-8") as handle:
                return handle.read()
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # -- list actions -------------------------------------------------------
    def current_row(self) -> ThreadRow | None:
        if 0 <= self.list_index < len(self.view):
            return self.view[self.list_index]
        return None

    def open_current(self) -> None:
        row = self.current_row()
        if row is None:
            return
        self.current_thread = row
        try:
            thread = self.get_thread(row)
        except ValueError as exc:
            self.message = f"Failed to decode: {exc}"
            return
        self.detail_search = ""
        self.selected.clear()
        self.detail_index = 0
        self.detail_offset = 0
        self.build_detail_rows(thread)
        self.state = STATE_DETAIL
        self.message = f"{len(thread.messages)} messages, {thread.part_count} parts"

    def list_delete(self) -> None:
        row = self.current_row()
        if row is None:
            return
        stage = self.stage_for(row.id)
        stage.op.delete = not stage.op.delete
        self.message = ("Marked for deletion: " if stage.op.delete else "Unmarked: ") + row.id[:8]

    def list_reassign(self) -> None:
        row = self.current_row()
        if row is None:
            return
        initial = row.folders[0] if row.folders else ""
        value = self.prompt("New project folder: ", initial)
        if value is None:
            return
        self.stage_for(row.id).op.folder = value
        self.message = f"Folder staged -> {value or '(none)'}"

    def list_set_model(self) -> None:
        row = self.current_row()
        if row is None:
            return
        try:
            provider, model = self.get_thread(row).next_model
        except ValueError:
            provider, model = (None, None)
        initial = f"{provider or ''}:{model or ''}"
        value = self.prompt("Next model (provider:model): ", initial)
        if value is None or ":" not in value:
            if value is not None:
                self.message = "Model must be provider:model"
            return
        prov, mod = value.split(":", 1)
        self.stage_for(row.id).op.set_model = (prov, mod)
        self.message = f"Model staged -> {prov}/{mod}"

    def list_set_title(self) -> None:
        row = self.current_row()
        if row is None:
            return
        value = self.prompt("New title: ", row.summary or "")
        if value is None:
            return
        self.stage_for(row.id).op.set_title = value
        self.message = f"Title staged -> {value}"

    # -- detail actions -----------------------------------------------------
    def current_detail(self) -> DetailRow | None:
        if 0 <= self.detail_index < len(self.detail_rows):
            return self.detail_rows[self.detail_index]
        return None

    def toggle_select(self) -> None:
        dr = self.current_detail()
        if dr is None or dr.kind != "part":
            return
        pid = dr.part.pid
        if pid in self.selected:
            self.selected.discard(pid)
        else:
            self.selected.add(pid)
        self.detail_index = min(self.detail_index + 1, len(self.detail_rows) - 1)

    def _target_pids(self) -> list[str]:
        if self.selected:
            return list(self.selected)
        dr = self.current_detail()
        if dr is not None and dr.kind == "part":
            return [dr.part.pid]
        return []

    def stage_part_action(self, mode: str, value: str | None = None) -> None:
        if self.current_thread is None:
            return
        stage = self.stage_for(self.current_thread.id)
        pids = self._target_pids()
        if not pids:
            return
        for pid in pids:
            if mode == ACTION_SET_TEXT:
                stage.part_actions[pid] = PartAction(mode=mode, set_text_value=value)
            else:
                stage.part_actions[pid] = PartAction(mode=mode)
        self.selected.clear()
        self.message = f"Staged {mode} on {len(pids)} part(s)."

    def detail_undo(self) -> None:
        if self.current_thread is None:
            return
        stage = self.stage.get(self.current_thread.id)
        if not stage:
            return
        for pid in self._target_pids():
            stage.part_actions.pop(pid, None)
        self.message = "Cleared staged change(s)."

    def detail_edit(self, *, editor: bool) -> None:
        dr = self.current_detail()
        if dr is None or dr.kind != "part":
            return
        part = dr.part
        if not part.editable:
            self.message = "This part type is not editable."
            return
        if editor:
            value = self.edit_in_editor(part.text())
        else:
            value = self.prompt("New text: ", part.preview(200))
        if value is None:
            return
        self.stage_part_action(ACTION_SET_TEXT, value)

    def detail_view_full(self) -> None:
        dr = self.current_detail()
        if dr is None or dr.kind != "part":
            return
        self.show_pager(dr.part.text(), f"{dr.part.kind} {dr.part.tool_name or ''}")

    def detail_select_all(self) -> None:
        for dr in self.detail_rows:
            if dr.kind == "part":
                self.selected.add(dr.part.pid)
        self.message = f"Selected {len(self.selected)} part(s)."

    def detail_find(self) -> None:
        value = self.prompt("Find in thread: ", self.detail_search)
        if value is None:
            return
        self.detail_search = value
        if self.current_thread is not None:
            self.build_detail_rows(self.get_thread(self.current_thread))
        self.detail_index = 0
        self.detail_offset = 0

    def toggle_detail_sort(self) -> None:
        order = ["document", "size", "length"]
        self.detail_sort = order[(order.index(self.detail_sort) + 1) % len(order)]
        if self.current_thread is not None:
            self.build_detail_rows(self.get_thread(self.current_thread))
        self.detail_index = 0
        self.detail_offset = 0
        self.message = f"Parts sorted by {self.detail_sort}."

    # -- writing ------------------------------------------------------------
    def commit(self) -> None:
        if self.read_only:
            self.message = "Database is read-only; cannot write."
            return
        dirty = {tid: s for tid, s in self.stage.items() if not s.empty}
        if not dirty:
            self.message = "Nothing staged to write."
            return
        if not self.confirm(f"Write {self.pending_count()} change(s)?"):
            self.message = "Write cancelled."
            return
        backup = backup_database(self.db.path)
        deleted = 0
        changed = 0
        for thread_id, stage in dirty.items():
            row = self._row_by_id(thread_id)
            if row is None:
                continue
            if stage.op.delete:
                self.db.delete_thread(thread_id)
                deleted += 1
                continue
            thread = self.get_thread(row)
            self._apply_stage(thread, stage)
            doc_dirty = bool(stage.part_actions) or self._op_touches_doc(stage.op)
            if doc_dirty:
                thread.apply()
                dtype = row.data_type if row.data_type in ("zstd", "json") else "zstd"
                self.db.update_thread_document(thread_id, row.doc(), data_type=dtype)
            meta = self._op_metadata(stage.op)
            if meta:
                self.db.update_metadata(thread_id, **meta)
            changed += 1
        self.db.commit()
        self.stage.clear()
        self.thread_cache.clear()
        self.reload()
        self.state = STATE_LIST
        self.message = f"Wrote {changed} thread(s), deleted {deleted}. Backup: {backup.name}"

    def _apply_stage(self, thread: Thread, stage: ThreadStage) -> None:
        pid_map = {part.pid: part for part in thread.iter_parts(include_dropped=True)}
        for pid, action in stage.part_actions.items():
            part = pid_map.get(pid)
            if part is not None:
                action.apply(part)
        op = stage.op
        if op.set_model is not None:
            thread.set_next_model(*op.set_model)
        if op.set_profile is not None:
            thread.set_profile(op.set_profile)
        if op.set_title is not None:
            thread.set_title(op.set_title)

    @staticmethod
    def _op_touches_doc(op: ThreadOp) -> bool:
        return op.set_model is not None or op.set_profile is not None or op.set_title is not None

    @staticmethod
    def _op_metadata(op: ThreadOp) -> dict[str, object]:
        meta: dict[str, object] = {}
        if op.set_title is not None:
            meta["summary"] = op.set_title
        if op.folder is not None:
            folders = [line for line in op.folder.splitlines() if line.strip()]
            joined = "\n".join(folders)
            meta["folder_paths"] = joined or None
            meta["folder_paths_order"] = joined or None
        return meta

    def _row_by_id(self, thread_id: str) -> ThreadRow | None:
        for row in self.rows:
            if row.id == thread_id:
                return row
        return self.db.get_thread(thread_id)

    # -- event loop ---------------------------------------------------------
    def run(self) -> None:
        curses.curs_set(0)
        while True:
            self.draw()
            key = self.stdscr.getch()
            if self.state == STATE_LIST:
                if not self.handle_list_key(key):
                    break
            elif not self.handle_detail_key(key):
                break

    def handle_list_key(self, key: int) -> bool:
        body = self.stdscr.getmaxyx()[0] - 3
        if key in (ord("q"),):
            return self._quit()
        if key in (curses.KEY_DOWN, ord("j")):
            self.list_index = min(self.list_index + 1, len(self.view) - 1)
        elif key in (curses.KEY_UP, ord("k")):
            self.list_index = max(self.list_index - 1, 0)
        elif key == curses.KEY_NPAGE:
            self.list_index = min(self.list_index + body, len(self.view) - 1)
        elif key == curses.KEY_PPAGE:
            self.list_index = max(self.list_index - body, 0)
        elif key in (curses.KEY_HOME, ord("g")):
            self.list_index = 0
        elif key in (curses.KEY_END, ord("G")):
            self.list_index = max(0, len(self.view) - 1)
        elif key in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT, ord("l")):
            self.open_current()
        elif key == ord("/"):
            value = self.prompt("Search threads: ", self.list_search)
            if value is not None:
                self.list_search = value
                self.apply_list_search()
        elif key == ord("d"):
            self.list_delete()
        elif key == ord("r"):
            self.list_reassign()
        elif key == ord("m"):
            self.list_set_model()
        elif key == ord("t"):
            self.list_set_title()
        elif key == ord("s"):
            self.cycle_list_sort()
        elif key == ord("w"):
            self.commit()
        elif key == ord("?"):
            self.show_pager(_HELP_TEXT, "Help")
        return True

    def handle_detail_key(self, key: int) -> bool:
        body = self.stdscr.getmaxyx()[0] - 3
        if key in (ord("q"), 27, curses.KEY_LEFT, ord("h")):
            self.state = STATE_LIST
            self.message = f"{len(self.view)} thread(s)."
            return True
        if key in (curses.KEY_DOWN, ord("j")):
            self.detail_index = min(self.detail_index + 1, len(self.detail_rows) - 1)
        elif key in (curses.KEY_UP, ord("k")):
            self.detail_index = max(self.detail_index - 1, 0)
        elif key == curses.KEY_NPAGE:
            self.detail_index = min(self.detail_index + body, len(self.detail_rows) - 1)
        elif key == curses.KEY_PPAGE:
            self.detail_index = max(self.detail_index - body, 0)
        elif key in (curses.KEY_HOME, ord("g")):
            self.detail_index = 0
        elif key in (curses.KEY_END, ord("G")):
            self.detail_index = max(0, len(self.detail_rows) - 1)
        elif key == ord(" "):
            self.toggle_select()
        elif key == ord("d"):
            self.stage_part_action(ACTION_PLACEHOLDER)
        elif key == ord("D"):
            self.stage_part_action(ACTION_REMOVE)
        elif key == ord("i"):
            self.stage_part_action(ACTION_STRIP_IMAGES)
        elif key == ord("e"):
            self.detail_edit(editor=False)
        elif key == ord("E"):
            self.detail_edit(editor=True)
        elif key == ord("u"):
            self.detail_undo()
        elif key == ord("a"):
            self.detail_select_all()
        elif key == ord("c"):
            self.selected.clear()
            self.message = "Selection cleared."
        elif key == ord("s"):
            self.toggle_detail_sort()
        elif key == ord("/"):
            self.detail_find()
        elif key in (curses.KEY_ENTER, 10, 13):
            self.detail_view_full()
        elif key == ord("w"):
            self.commit()
        return True

    def _quit(self) -> bool:
        if self.pending_count() and not self.read_only:
            if self.confirm("Discard staged changes and quit?"):
                return False
            return True
        return False


_HELP_TEXT = """Zed Context Manipulator - TUI help

Thread list:
  j / k, arrows   move
  PgUp / PgDn     page
  g / G           top / bottom
  Enter / l       open thread
  /               search (title, project, content)
  s               cycle sort (updated / size / title)
  d               toggle delete mark
  r               reassign project folder
  m               change next model (provider:model)
  t               change title
  w               write staged changes (makes a backup)
  q               quit

Thread detail:
  j / k, arrows   move
  space           select / deselect part
  a / c           select all / clear selection
  d               stage drop (replace with placeholder)
  D               stage remove (delete the part entirely)
  i               stage strip-images (keep text, drop image data)
  e               edit text inline
  E               edit text in $EDITOR
  u               clear staged change on selection/part
  /               find within the thread
  s               cycle sort (document / size / length)
  Enter           view full part text
  w               write staged changes
  q / h / Esc     back to list

All changes are staged in memory and only written on 'w', which always
creates a timestamped .bak copy of the database first.
"""


def _short_size(num: int) -> str:
    if num < 1024:
        return f"{num}B"
    if num < 1024 * 1024:
        return f"{num / 1024:.0f}K"
    return f"{num / (1024 * 1024):.1f}M"


def run_tui(path: Path, query: ThreadQuery, *, read_only: bool = False) -> int:
    """Launch the curses UI against the database at ``path``."""

    db = ThreadDatabase(path, read_only=read_only)

    def _main(stdscr) -> None:
        App(stdscr, db, query, read_only=read_only).run()

    try:
        curses.wrapper(_main)
    finally:
        db.close()
    return 0
