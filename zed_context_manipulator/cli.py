"""Command-line interface for Zed Context Manipulator."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Sequence

from . import __version__
from .database import ThreadDatabase, backup_database, resolve_db_path
from .filters import PartQuery, PositionSelector, ThreadQuery
from .operations import (
    ACTION_PLACEHOLDER,
    ACTION_REMOVE,
    ACTION_REPLACE,
    ACTION_SET_TEXT,
    ACTION_STRIP_IMAGES,
    PartAction,
    RunReport,
    ThreadOp,
    execute,
    matching_threads,
)
from .threadmodel import ALL_KINDS, Thread, format_label

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
}


def parse_size(text: str) -> int:
    """Parse a byte count that may carry a K/M/G/T suffix (e.g. '2.5M')."""

    match = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*", text or "")
    if not match:
        raise argparse.ArgumentTypeError(f"invalid size: {text!r}")
    number, suffix = match.group(1), match.group(2).lower()
    if suffix not in _SIZE_UNITS:
        raise argparse.ArgumentTypeError(f"unknown size unit: {suffix!r}")
    return int(float(number) * _SIZE_UNITS[suffix])


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _split_csv(values: Sequence[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        out.extend(piece.strip() for piece in value.split(",") if piece.strip())
    return out


def build_thread_query(args: argparse.Namespace) -> ThreadQuery:
    ignore_case = not getattr(args, "case_sensitive", False)
    thread_ids = set(_split_csv(getattr(args, "thread_id", None))) or None
    return ThreadQuery(
        thread_ids=thread_ids,
        title_contains=getattr(args, "title_contains", None),
        title_regex=getattr(args, "title_regex", None),
        content_contains=getattr(args, "content_contains", None),
        content_regex=getattr(args, "content_regex", None),
        folder_contains=getattr(args, "folder", None),
        folder_glob=getattr(args, "folder_glob", None),
        project=getattr(args, "project", None),
        model_contains=getattr(args, "model", None),
        model_regex=getattr(args, "model_regex", None),
        profile=getattr(args, "profile", None),
        parent_id=getattr(args, "parent_id", None),
        created_after=getattr(args, "created_after", None),
        created_before=getattr(args, "created_before", None),
        updated_after=getattr(args, "updated_after", None),
        updated_before=getattr(args, "updated_before", None),
        has_images=_tristate(getattr(args, "has_images", None), getattr(args, "no_images", None)),
        min_messages=getattr(args, "min_messages", None),
        max_messages=getattr(args, "max_messages", None),
        min_size=getattr(args, "min_thread_size", None),
        max_size=getattr(args, "max_thread_size", None),
        ignore_case=ignore_case,
    )


def _tristate(positive: bool | None, negative: bool | None) -> bool | None:
    if positive:
        return True
    if negative:
        return False
    return None


def build_part_query(args: argparse.Namespace) -> PartQuery:
    ignore_case = not getattr(args, "case_sensitive", False)
    roles = set(_split_csv(getattr(args, "role", None))) or None
    kinds = set(_split_csv(getattr(args, "type", None))) or None
    tools = set(_split_csv(getattr(args, "tool", None))) or None
    return PartQuery(
        roles=roles,
        kinds=kinds,
        tool_names=tools,
        path_globs=_split_csv(getattr(args, "path_glob", None)),
        path_regex=getattr(args, "path_regex", None),
        target_regex=getattr(args, "target_regex", None),
        content_contains=getattr(args, "part_contains", None),
        content_regex=getattr(args, "part_regex", None),
        is_error=_tristate(getattr(args, "errors_only", None), getattr(args, "no_errors", None)),
        images_only=bool(getattr(args, "images_only", False)),
        min_size=getattr(args, "min_size", None),
        max_size=getattr(args, "max_size", None),
        min_length=getattr(args, "min_length", None),
        max_length=getattr(args, "max_length", None),
        ignore_case=ignore_case,
    )


def build_position(args: argparse.Namespace) -> PositionSelector:
    return PositionSelector(
        oldest=getattr(args, "oldest", None),
        newest=getattr(args, "newest", None),
        index_min=getattr(args, "index_min", None),
        index_max=getattr(args, "index_max", None),
        keep_oldest=getattr(args, "keep_oldest", None),
        keep_newest=getattr(args, "keep_newest", None),
        middle=bool(getattr(args, "middle", False)),
    )


def open_db(args: argparse.Namespace, *, read_only: bool) -> ThreadDatabase:
    path = resolve_db_path(getattr(args, "db", None))
    return ThreadDatabase(path, read_only=read_only)


# ---------------------------------------------------------------------------
# Argument parser construction
# ---------------------------------------------------------------------------


def _add_thread_filter(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("thread filters")
    group.add_argument("--thread-id", action="append", help="Thread id (repeatable, CSV ok)")
    group.add_argument("--title-contains", help="Match threads whose title contains text")
    group.add_argument("--title-regex", help="Match threads whose title matches a regex")
    group.add_argument(
        "--content-contains", help="Match threads whose title or any message contains text"
    )
    group.add_argument("--content-regex", help="Match thread title/content against a regex")
    group.add_argument("--folder", help="Match threads whose project folder contains text")
    group.add_argument("--folder-glob", help="Match a project folder path against a glob")
    group.add_argument("--project", help="Match threads by project folder basename")
    group.add_argument("--model", help="Match threads by next-model text (provider/model)")
    group.add_argument("--model-regex", help="Match next-model against a regex")
    group.add_argument("--profile", help="Match threads by agent profile")
    group.add_argument("--parent-id", help="Match threads by parent id")
    group.add_argument("--created-after", help="Match threads created after DATE (ISO 8601)")
    group.add_argument("--created-before", help="Match threads created before DATE")
    group.add_argument("--updated-after", help="Match threads updated after DATE")
    group.add_argument("--updated-before", help="Match threads updated before DATE")
    group.add_argument("--has-images", action="store_true", help="Only threads containing images")
    group.add_argument("--no-images", action="store_true", help="Only threads without images")
    group.add_argument("--min-messages", type=int, help="Only threads with >= N messages")
    group.add_argument("--max-messages", type=int, help="Only threads with <= N messages")
    group.add_argument(
        "--min-thread-size",
        type=parse_size,
        metavar="SIZE",
        help="Only threads >= this stored size (accepts K/M/G suffixes)",
    )
    group.add_argument(
        "--max-thread-size",
        type=parse_size,
        metavar="SIZE",
        help="Only threads <= this stored size (accepts K/M/G suffixes)",
    )
    group.add_argument("--limit", type=int, help="Stop after N matching threads")


def _add_part_filter(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("part filters")
    group.add_argument(
        "--role", action="append", choices=["user", "assistant"], help="Restrict by message role"
    )
    group.add_argument(
        "--type", action="append", choices=list(ALL_KINDS), help="Restrict by part type"
    )
    group.add_argument("--tool", action="append", help="Restrict by tool name (repeatable, CSV)")
    group.add_argument("--path-glob", action="append", help="Match tool target path by glob")
    group.add_argument("--path-regex", help="Match tool target path by regex")
    group.add_argument("--target-regex", help="Match any tool target (path/url/cmd) by regex")
    group.add_argument("--part-contains", help="Match parts whose text contains this")
    group.add_argument("--part-regex", help="Match parts whose text matches a regex")
    group.add_argument("--errors-only", action="store_true", help="Only tool results with errors")
    group.add_argument("--no-errors", action="store_true", help="Only tool results without errors")
    group.add_argument("--images-only", action="store_true", help="Only parts that carry images")
    group.add_argument(
        "--min-size", type=parse_size, metavar="SIZE", help="Only parts >= this size (K/M/G ok)"
    )
    group.add_argument(
        "--max-size", type=parse_size, metavar="SIZE", help="Only parts <= this size (K/M/G ok)"
    )
    group.add_argument(
        "--min-length", type=int, metavar="CHARS", help="Only parts with >= N characters of text"
    )
    group.add_argument(
        "--max-length", type=int, metavar="CHARS", help="Only parts with <= N characters of text"
    )


def _add_position(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("message position")
    group.add_argument("--oldest", type=int, help="Select the oldest N messages")
    group.add_argument("--newest", type=int, help="Select the newest N messages")
    group.add_argument("--index-min", type=int, help="Select messages with index >= N")
    group.add_argument("--index-max", type=int, help="Select messages with index <= N")
    group.add_argument(
        "--middle",
        action="store_true",
        help="Select middle messages (combine with --keep-oldest/--keep-newest)",
    )
    group.add_argument("--keep-oldest", type=int, help="With --middle, protect first N messages")
    group.add_argument("--keep-newest", type=int, help="With --middle, protect last N messages")


def _add_write_opts(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("write options")
    group.add_argument("--write", action="store_true", help="Persist changes (default: dry run)")
    group.add_argument("--no-backup", action="store_true", help="Do not back up before writing")
    group.add_argument("--vacuum", action="store_true", help="Run VACUUM after writing")
    group.add_argument("--touch", action="store_true", help="Bump updated_at on changed threads")
    group.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zed-context-manipulator",
        description="Search, filter, edit, and prune Zed agent conversation threads.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--db", help="Path to threads.db (default: Zed's data dir)")
    parser.add_argument(
        "-s", "--case-sensitive", action="store_true", help="Make all matching case-sensitive"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List threads")
    _add_thread_filter(p_list)
    p_list.add_argument(
        "--sort",
        choices=["updated", "created", "title", "size", "messages"],
        default="updated",
    )
    p_list.add_argument("--reverse", action="store_true", help="Reverse sort order")
    p_list.add_argument("--format", choices=["table", "json", "ids"], default="table")
    p_list.add_argument(
        "--count-messages", action="store_true", help="Decode payloads to show message counts"
    )
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show a thread's messages and parts")
    p_show.add_argument("thread", nargs="?", help="Thread id (or use thread filters)")
    _add_thread_filter(p_show)
    _add_part_filter(p_show)
    p_show.add_argument("--message", type=int, help="Only show this message index")
    p_show.add_argument("--full", action="store_true", help="Show full part text, not previews")
    p_show.add_argument(
        "--sort",
        choices=["document", "size", "length", "kind"],
        default="document",
        help="Order parts by document position (default), size, length, or type",
    )
    p_show.add_argument("--reverse", action="store_true", help="Reverse the part sort order")
    p_show.set_defaults(func=cmd_show)

    p_search = sub.add_parser("search", help="Search across threads and parts")
    p_search.add_argument("query", help="Text to search for (regex with --regex)")
    p_search.add_argument("--regex", action="store_true", help="Treat query as a regex")
    _add_thread_filter(p_search)
    _add_part_filter(p_search)
    p_search.add_argument("--max-per-thread", type=int, default=5, help="Cap matches per thread")
    p_search.set_defaults(func=cmd_search)

    p_stats = sub.add_parser("stats", help="Show aggregate statistics")
    _add_thread_filter(p_stats)
    p_stats.set_defaults(func=cmd_stats)

    p_drop = sub.add_parser("drop", help="Drop or prune parts (placeholder/remove/strip-images)")
    _add_thread_filter(p_drop)
    _add_part_filter(p_drop)
    _add_position(p_drop)
    _add_write_opts(p_drop)
    p_drop.add_argument(
        "--mode",
        choices=[ACTION_PLACEHOLDER, ACTION_REMOVE, ACTION_STRIP_IMAGES],
        default=ACTION_PLACEHOLDER,
        help="How to drop (default: placeholder)",
    )
    p_drop.add_argument(
        "--images",
        action="store_true",
        help="Preset: target images and strip them (mode defaults to strip-images)",
    )
    p_drop.add_argument(
        "--keep-latest-images",
        type=int,
        metavar="N",
        help="Keep the newest N image-bearing parts per thread",
    )
    p_drop.add_argument("--invert", action="store_true", help="Act on parts that do NOT match")
    p_drop.add_argument(
        "--all", action="store_true", help="Allow dropping with no part/position filter"
    )
    p_drop.set_defaults(func=cmd_drop)

    p_edit = sub.add_parser("edit", help="Edit/replace text in matching parts")
    _add_thread_filter(p_edit)
    _add_part_filter(p_edit)
    _add_position(p_edit)
    _add_write_opts(p_edit)
    p_edit.add_argument("--replace", metavar="REGEX", help="Regex to replace within part text")
    p_edit.add_argument("--with", dest="replace_with", default="", help="Replacement text")
    p_edit.add_argument("--set-text", help="Replace entire part text with this value")
    p_edit.add_argument("--invert", action="store_true", help="Act on parts that do NOT match")
    p_edit.set_defaults(func=cmd_edit)

    p_thread = sub.add_parser("thread", help="Thread-level operations")
    _add_thread_filter(p_thread)
    _add_write_opts(p_thread)
    p_thread.add_argument("--delete", action="store_true", help="Delete matching threads")
    p_thread.add_argument("--reassign", metavar="FOLDER", help="Set project folder path")
    p_thread.add_argument(
        "--set-model", metavar="PROVIDER:MODEL", help="Set the next model (e.g. zed.dev:gpt-5.5)"
    )
    p_thread.add_argument("--set-profile", metavar="NAME", help="Set the agent profile")
    p_thread.add_argument("--set-title", metavar="TITLE", help="Set the thread title")
    p_thread.set_defaults(func=cmd_thread)

    p_backup = sub.add_parser("backup", help="Create a timestamped backup of the database")
    p_backup.set_defaults(func=cmd_backup)

    p_tui = sub.add_parser("tui", help="Launch the interactive terminal UI")
    _add_thread_filter(p_tui)
    p_tui.add_argument("--read-only", action="store_true", help="Open the database read-only")
    p_tui.set_defaults(func=cmd_tui)

    return parser


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    query = build_thread_query(args)
    with open_db(args, read_only=True) as db:
        rows = db.load_threads()
        pairs = matching_threads(rows, query)
        if args.limit:
            pairs = pairs[: args.limit]
        records = []
        for row, thread in pairs:
            if thread is None and (args.count_messages or args.sort == "messages"):
                thread = Thread(row.doc())
            msgs = len(thread.messages) if thread is not None else None
            provider, model = thread.next_model if thread is not None else (None, None)
            records.append(
                {
                    "id": row.id,
                    "title": row.summary or "(untitled)",
                    "updated_at": row.updated_at,
                    "created_at": row.created_at,
                    "project": ", ".join(row.folders) or "-",
                    "size": row.raw_size,
                    "messages": msgs,
                    "model": f"{provider or ''}/{model or ''}" if provider or model else None,
                }
            )
        descending = args.sort in ("updated", "created", "size", "messages")
        if args.reverse:
            descending = not descending
        records.sort(key=_sort_key(args.sort), reverse=descending)
        if args.format == "ids":
            for rec in records:
                print(rec["id"])
        elif args.format == "json":
            print(json.dumps(records, indent=2, default=str))
        else:
            _print_thread_table(records, show_messages=args.count_messages)
        print(f"\n{len(records)} thread(s).", file=sys.stderr)
    return 0


def _sort_key(field: str):
    def key(rec: dict):
        if field == "title":
            return (rec["title"] or "").lower()
        if field == "size":
            return rec["size"] or 0
        if field == "messages":
            return rec["messages"] or 0
        if field == "created":
            return rec["created_at"] or ""
        return rec["updated_at"] or ""

    return key


def _print_thread_table(records: list[dict], *, show_messages: bool) -> None:
    if not records:
        print("No threads matched.")
        return
    for rec in records:
        date = (rec["updated_at"] or "")[:19].replace("T", " ")
        line = f"{rec['id'][:8]}  {date:<19}  {human_size(rec['size']):>9}"
        if show_messages and rec["messages"] is not None:
            line += f"  {rec['messages']:>4} msgs"
        if rec["model"]:
            line += f"  [{rec['model']}]"
        print(line)
        print(f"    {rec['title']}")
        if rec["project"] != "-":
            print(f"    \u2514 {rec['project']}")


def _select_single_thread(db: ThreadDatabase, args: argparse.Namespace):
    if getattr(args, "thread", None):
        row = db.get_thread(args.thread)
        if row is None:
            ids = [r.id for r in db.load_threads() if r.id.startswith(args.thread)]
            if len(ids) == 1:
                row = db.get_thread(ids[0])
        return row
    query = build_thread_query(args)
    pairs = matching_threads(db.load_threads(), query)
    return pairs[0][0] if pairs else None


def cmd_show(args: argparse.Namespace) -> int:
    part_query = build_part_query(args)
    with open_db(args, read_only=True) as db:
        row = _select_single_thread(db, args)
        if row is None:
            print("No matching thread found.", file=sys.stderr)
            return 1
        thread = Thread(row.doc())
        provider, model = thread.next_model
        print(f"Thread {row.id}")
        print(f"Title:   {row.summary or thread.title or '(untitled)'}")
        print(f"Project: {', '.join(row.folders) or '-'}")
        print(f"Updated: {row.updated_at}")
        print(f"Model:   {provider or ''}/{model or ''}   profile={thread.profile or '-'}")
        print(f"Messages: {len(thread.messages)}   Parts: {thread.part_count}")
        print("-" * 78)
        parts = []
        for message in thread.messages:
            if args.message is not None and message.index != args.message:
                continue
            for part in message.iter_parts():
                if part_query.is_empty or part_query.matches(part):
                    parts.append(part)
        sort = getattr(args, "sort", "document")
        if sort == "document":
            current = None
            for part in parts:
                if part.message_index != current:
                    current = part.message_index
                    msg = thread.messages[current]
                    print(f"[{msg.index}] {msg.role.upper()} ({format_label(msg.fmt)})")
                _print_part(part, full=args.full)
        else:
            if sort == "size":
                parts.sort(key=lambda p: p.size(), reverse=not args.reverse)
            elif sort == "length":
                parts.sort(key=lambda p: p.length(), reverse=not args.reverse)
            else:  # kind
                parts.sort(key=lambda p: (p.kind, -p.size()), reverse=args.reverse)
            for part in parts:
                _print_part(part, full=args.full)
    return 0


def _print_part(part, *, full: bool) -> None:
    tag = part.kind
    if part.tool_name:
        tag += f":{part.tool_name}"
    extra = ""
    if part.target:
        extra += f" -> {part.target}"
    if part.is_error:
        extra += " [error]"
    flags = " [img]" if part.has_image else ""
    meta = human_size(part.size())
    if not part.has_image:
        meta += f", {part.length()} chars"
    print(f"  - {part.pid}  {tag}{flags}  ({meta}){extra}")
    text = part.text()
    if not text:
        return
    if full:
        for line in text.splitlines():
            print(f"      {line}")
    else:
        print(f"      {part.preview(100)}")


def cmd_search(args: argparse.Namespace) -> int:
    flags = 0 if args.case_sensitive else re.IGNORECASE
    if args.regex:
        pattern = re.compile(args.query, flags)
    else:
        pattern = re.compile(re.escape(args.query), flags)
    part_query = build_part_query(args)
    thread_query = build_thread_query(args)
    total = 0
    with open_db(args, read_only=True) as db:
        for row in db.load_threads():
            thread = Thread(row.doc())
            if not thread_query.matches(row, thread):
                continue
            hits = []
            for part in thread.iter_parts():
                if not part_query.is_empty and not part_query.matches(part):
                    continue
                text = part.text()
                if pattern.search(text) or pattern.search(row.summary or ""):
                    hits.append(part)
                if len(hits) >= args.max_per_thread:
                    break
            if not hits:
                continue
            total += len(hits)
            print(f"\n{row.id[:8]}  {row.summary or '(untitled)'}")
            if row.folders:
                print(f"    \u2514 {', '.join(row.folders)}")
            for part in hits:
                tag = part.kind + (f":{part.tool_name}" if part.tool_name else "")
                print(f"    [{part.pid}] {tag}: {_highlight(part.preview(120), pattern)}")
    print(f"\n{total} match(es).", file=sys.stderr)
    return 0


def _highlight(text: str, pattern) -> str:
    return pattern.sub(lambda m: f"\033[1;33m{m.group(0)}\033[0m", text)


def cmd_stats(args: argparse.Namespace) -> int:
    query = build_thread_query(args)
    with open_db(args, read_only=True) as db:
        rows = db.load_threads()
        pairs = matching_threads(rows, query)
        projects: Counter[str] = Counter()
        models: Counter[str] = Counter()
        tools: Counter[str] = Counter()
        kinds: Counter[str] = Counter()
        total_messages = 0
        image_parts = 0
        image_bytes = 0
        for row, thread in pairs:
            if thread is None:
                thread = Thread(row.doc())
            for folder in row.folders or ["(none)"]:
                projects[folder] += 1
            provider, model = thread.next_model
            models[f"{provider or '?'}/{model or '?'}"] += 1
            total_messages += len(thread.messages)
            for part in thread.iter_parts():
                kinds[part.kind] += 1
                if part.tool_name:
                    tools[part.tool_name] += 1
                if part.has_image:
                    image_parts += 1
                    image_bytes += part.size()
        print(f"Database:  {db.path}")
        print(f"Threads:   {len(pairs)} (of {len(rows)} total)")
        print(f"Messages:  {total_messages}")
        print(f"Images:    {image_parts} parts, ~{human_size(image_bytes)}")
        _print_counter("Top projects", projects, 10)
        _print_counter("Next models", models, 10)
        _print_counter("Tools used", tools, 15)
        _print_counter("Part types", kinds, 10)
    return 0


def _print_counter(title: str, counter: Counter, limit: int) -> None:
    print(f"\n{title}:")
    if not counter:
        print("  (none)")
        return
    for name, count in counter.most_common(limit):
        print(f"  {count:>6}  {name}")


def _print_run_report(report: RunReport, *, verbose: bool = True) -> None:
    for thread_report in report.threads:
        if not thread_report.changed:
            continue
        print(f"\n{thread_report.thread_id[:8]}  {thread_report.title}")
        for action in thread_report.thread_actions:
            print(f"    * {action}")
        shown = thread_report.changes if verbose else thread_report.changes[:5]
        for change in shown:
            tag = change.kind + (f":{change.tool_name}" if change.tool_name else "")
            target = f" -> {change.target}" if change.target else ""
            print(f"    [{change.pid}] {change.action} {tag}{target}")
        hidden = len(thread_report.changes) - len(shown)
        if hidden > 0:
            print(f"    ... and {hidden} more part change(s)")
    mode = "WROTE" if report.write else "DRY RUN"
    print("\n" + "=" * 60)
    print(
        f"{mode}: {len(report.changed_threads)} thread(s) changed, "
        f"{report.total_part_changes} part change(s), "
        f"{report.deleted_threads} thread(s) deleted."
    )
    print(f"Scanned {report.scanned} matching thread(s).")
    if report.backup_path:
        print(f"Backup: {report.backup_path}")
    if report.vacuumed:
        print("Database vacuumed.")
    if not report.write:
        print("Dry run only. Re-run with --write to apply.")


def _run_action(args: argparse.Namespace, action: PartAction | None, thread_op: ThreadOp) -> int:
    thread_query = build_thread_query(args)
    part_query = build_part_query(args)
    position = build_position(args)
    invert = bool(getattr(args, "invert", False))
    keep_latest = getattr(args, "keep_latest_images", None)
    write = bool(getattr(args, "write", False))
    destructive = thread_op.delete or (action is not None and action.mode == ACTION_REMOVE)
    if write and destructive and not getattr(args, "yes", False):
        if not _confirm("This will permanently remove data. Continue?"):
            print("Aborted.", file=sys.stderr)
            return 1
    with open_db(args, read_only=not write) as db:
        rows = db.load_threads()
        report = execute(
            db,
            rows,
            thread_query=thread_query,
            position=position,
            part_query=part_query,
            action=action,
            invert=invert,
            keep_latest_images=keep_latest,
            thread_op=thread_op,
            write=write,
            backup=not getattr(args, "no_backup", False),
            vacuum=bool(getattr(args, "vacuum", False)),
            touch=bool(getattr(args, "touch", False)),
            limit=getattr(args, "limit", None),
        )
        _print_run_report(report)
    return 0


def cmd_drop(args: argparse.Namespace) -> int:
    part_query = build_part_query(args)
    position = build_position(args)
    if args.images and not args.images_only:
        part_query.images_only = True
    mode = args.mode
    if args.images and args.mode == ACTION_PLACEHOLDER:
        mode = ACTION_STRIP_IMAGES
    no_selectors = (
        part_query.is_empty
        and position.is_empty
        and not args.images
        and getattr(args, "keep_latest_images", None) is None
    )
    if no_selectors and not args.all:
        print(
            "Refusing to drop with no part/position filter. "
            "Add a filter, or pass --all to target everything.",
            file=sys.stderr,
        )
        return 2
    action = PartAction(
        mode=mode,
        ignore_case=not args.case_sensitive,
    )
    # Re-inject the images preset into the namespace-derived query.
    args.images_only = part_query.images_only
    return _run_action(args, action, ThreadOp())


def cmd_edit(args: argparse.Namespace) -> int:
    if args.replace is None and args.set_text is None:
        print("Provide --replace REGEX --with TEXT, or --set-text TEXT.", file=sys.stderr)
        return 2
    if args.set_text is not None:
        action = PartAction(mode=ACTION_SET_TEXT, set_text_value=args.set_text)
    else:
        action = PartAction(
            mode=ACTION_REPLACE,
            replace_pattern=args.replace,
            replace_with=args.replace_with,
            ignore_case=not args.case_sensitive,
        )
    return _run_action(args, action, ThreadOp())


def cmd_thread(args: argparse.Namespace) -> int:
    set_model = None
    if args.set_model is not None:
        if ":" not in args.set_model:
            print("--set-model must be PROVIDER:MODEL (e.g. zed.dev:gpt-5.5).", file=sys.stderr)
            return 2
        provider, model = args.set_model.split(":", 1)
        set_model = (provider, model)
    thread_op = ThreadOp(
        delete=bool(args.delete),
        folder=args.reassign,
        set_model=set_model,
        set_profile=args.set_profile,
        set_title=args.set_title,
    )
    if thread_op.is_empty:
        print(
            "Specify at least one of --delete/--reassign/--set-model/--set-profile/--set-title.",
            file=sys.stderr,
        )
        return 2
    return _run_action(args, None, thread_op)


def cmd_backup(args: argparse.Namespace) -> int:
    path = resolve_db_path(getattr(args, "db", None))
    if not path.exists():
        print(f"Database not found: {path}", file=sys.stderr)
        return 1
    backup_path = backup_database(path)
    print(f"Backup written: {backup_path}")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    from .tui import run_tui

    query = build_thread_query(args)
    path = resolve_db_path(getattr(args, "db", None))
    return run_tui(path, query, read_only=bool(args.read_only))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
