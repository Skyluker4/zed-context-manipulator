#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
import sys

import zstandard as zstd


PLACEHOLDER = "[Previous read_file image omitted from Zed thread history to reduce request payload. Re-run read_file if the image is needed again.]"


def decode_thread(data_type, data):
    if data_type == "zstd":
        try:
            raw = zstd.ZstdDecompressor().decompress(data)
        except zstd.ZstdError:
            with zstd.ZstdDecompressor().stream_reader(data) as reader:
                raw = reader.read()
        return json.loads(raw.decode("utf-8"))

    if data_type == "json":
        return json.loads(data.decode("utf-8"))

    raise ValueError(f"unknown data_type: {data_type}")

def encode_thread(doc):
    raw = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return zstd.ZstdCompressor(level=3).compress(raw)


def is_image_part(part):
    if not isinstance(part, dict):
        return False

    lowered = {str(k).lower(): v for k, v in part.items()}

    # Normal serde shape for LanguageModelToolResultContent::Image:
    # {"Image": {"source": "..."}}
    if "image" in lowered:
        return True

    # Deserializer also accepts direct image objects:
    # {"source": "..."}
    if "source" in lowered and "text" not in lowered:
        return True

    return False


def text_placeholder():
    # This shape is accepted by Zed's deserializer as a text tool-result part.
    return {"Text": PLACEHOLDER}


def result_has_image(result):
    content = result.get("content")
    if isinstance(content, list):
        return any(is_image_part(part) for part in content)
    return is_image_part(content)


def prune_result(result):
    content = result.get("content")

    if isinstance(content, list):
        result["content"] = [
            text_placeholder() if is_image_part(part) else part
            for part in content
        ]
    elif is_image_part(content):
        result["content"] = [text_placeholder()]

    # read_file's raw output can also contain serialized image data.
    result["output"] = None


def iter_agent_tool_results(thread_doc):
    for msg in thread_doc.get("messages", []):
        if not isinstance(msg, dict):
            continue

        # Current serde shape for Message::Agent is usually:
        # {"Agent": {"content": [...], "tool_results": {...}}}
        agent = msg.get("Agent") or msg.get("agent")
        if not isinstance(agent, dict):
            continue

        tool_results = agent.get("tool_results")
        if isinstance(tool_results, dict):
            for result in tool_results.values():
                if isinstance(result, dict):
                    yield result
        elif isinstance(tool_results, list):
            for result in tool_results:
                if isinstance(result, dict):
                    yield result


def matching_read_file_image_results(thread_doc):
    results = []
    for result in iter_agent_tool_results(thread_doc):
        if result.get("tool_name") == "read_file" and result_has_image(result):
            results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Remove older read_file image payloads from Zed native agent thread history."
    )
    parser.add_argument("db", help="Path to Zed threads.db")
    parser.add_argument("--thread-id", help="Only modify this thread id")
    parser.add_argument("--title-contains", help="Only modify threads whose summary/title contains this text")
    parser.add_argument(
        "--all-images",
        action="store_true",
        help="Remove all read_file images. By default, keeps the latest read_file image per thread.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually modify the database. Without this, performs a dry run.",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="Run VACUUM after writing, to shrink the SQLite file on disk.",
    )
    args = parser.parse_args()

    db_path = os.path.expanduser(os.path.expandvars(args.db))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    query = "SELECT id, summary, data_type, data FROM threads"
    params = []

    filters = []
    if args.thread_id:
        filters.append("id = ?")
        params.append(args.thread_id)
    if args.title_contains:
        filters.append("summary LIKE ?")
        params.append(f"%{args.title_contains}%")

    if filters:
        query += " WHERE " + " AND ".join(filters)

    rows = conn.execute(query, params).fetchall()

    changed_threads = 0
    pruned_images = 0

    for row in rows:
        thread_id = row["id"]
        summary = row["summary"]
        doc = decode_thread(row["data_type"], row["data"])

        results = matching_read_file_image_results(doc)
        if not results:
            continue

        to_prune = results if args.all_images else results[:-1]

        if not to_prune:
            print(f"KEEP   {thread_id}  {summary!r}  found 1 read_file image; keeping latest")
            continue

        for result in to_prune:
            prune_result(result)

        changed_threads += 1
        pruned_images += len(to_prune)

        print(
            f"{'WRITE' if args.write else 'DRY'}   {thread_id}  {summary!r}  "
            f"prune={len(to_prune)} keep_latest={not args.all_images}"
        )

        if args.write:
            encoded = encode_thread(doc)
            conn.execute(
                "UPDATE threads SET data_type = ?, data = ? WHERE id = ?",
                ("zstd", encoded, thread_id),
            )

    if args.write:
        conn.commit()
        if args.vacuum:
            conn.execute("VACUUM")
            conn.commit()

    print()
    print(f"Threads changed: {changed_threads}")
    print(f"read_file image results pruned: {pruned_images}")
    if not args.write:
        print("Dry run only. Re-run with --write to modify the database.")

    conn.close()


if __name__ == "__main__":
    main()
