# Zed Context Manipulator

Search, filter, edit, and prune the agent conversation **threads** that the
[Zed](https://zed.dev) editor stores locally. It ships both a scriptable
command-line interface and an interactive terminal UI (TUI).

Zed keeps every agent thread in a single SQLite database
(`threads.db`), with each thread's messages stored as a compressed JSON blob.
Over time these grow large -- especially when the agent reads images or large
files -- bloating the database and inflating the context that gets re-sent to
the model. This tool lets you reclaim that space and curate your history
surgically, without hand-editing SQLite.

> [!WARNING]
> This program edits Zed's private database. **Quit Zed before writing
> changes.** Every write is a *dry run* unless you pass `--write`, and every
> write makes a timestamped `.bak` copy first. When in doubt, test against a
> copy (`cp threads.db /tmp/threads.db` then `--db /tmp/threads.db`).

---

## Table of contents

- [Features](#features)
- [Installation](#installation)
- [Quick start](#quick-start)
- [How Zed stores threads](#how-zed-stores-threads)
- [Concepts: threads, messages, parts](#concepts-threads-messages-parts)
- [Command-line interface](#command-line-interface)
  - [Global options](#global-options)
  - [`list`](#list)
  - [`show`](#show)
  - [`search`](#search)
  - [`stats`](#stats)
  - [`drop`](#drop)
  - [`edit`](#edit)
  - [`thread`](#thread)
  - [`backup`](#backup)
  - [`tui`](#tui)
- [Filter reference](#filter-reference)
- [Recipes](#recipes)
- [TUI guide](#tui-guide)
- [Development](#development)
- [License](#license)

---

## Features

- **Full-text search** across thread titles *and* message content, with plain
  or regular-expression matching.
- **Rich filtering** by thread id, title, content, project folder, AI model,
  agent profile, parent thread, creation/update date, message count, and
  whether a thread contains images.
- **Part-level targeting** -- act on individual pieces of a conversation:
  user/assistant text, thinking blocks, images, tool calls, tool results, and
  context mentions.
- **Drop reads and other tool calls** by tool name, by file **path glob** or
  **regex**, by URL/command/query, by size, or by error status.
- **Positional pruning** -- target the oldest *N* messages, the newest *N*, an
  index range, or "everything in the middle" while protecting the head/tail.
- **Three drop modes**: replace with a lightweight `placeholder`, fully
  `remove`, or `strip-images` (keep the text of a tool result but discard the
  image bytes). Keep the newest *N* images per thread with
  `--keep-latest-images`.
- **Edit and replace** part text programmatically (regex `--replace`) or
  literally (`--set-text`), in bulk across matching threads.
- **Thread management**: delete entire threads, reassign them to another
  project folder, rename them, change the agent profile, or change **which
  model runs next**.
- **Interactive TUI** to browse, search, select parts, stage changes, and
  write them -- with `$EDITOR` integration for long edits.
- **Safe by default**: dry-run unless `--write`, automatic timestamped backup
  before every write, optional `VACUUM` to shrink the file.
- Handles **both** of Zed's on-disk thread encodings transparently and
  preserves fields it does not understand.

---

## Installation

Requires **Python 3.11+**. The only runtime dependency is
[`zstandard`](https://pypi.org/project/zstandard/).

### With `pipx` (recommended)

```sh
pipx install zed-context-manipulator
```

### With `pip`

```sh
pip install zed-context-manipulator
```

### From source (development)

```sh
git clone https://github.com/Skyluker4/zed-context-manipulator.git
cd zed-context-manipulator
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

Both `zed-context-manipulator` and the short alias `zcm` are installed.

---

## Quick start

```sh
# What's in my database?
zcm stats

# List threads, newest first.
zcm list

# Search every thread for a phrase.
zcm search "slot conflict"

# Preview pruning every read_file image but keep the newest one per thread.
zcm drop --images --keep-latest-images 1

# Actually do it (quit Zed first!), shrinking the file afterwards.
zcm drop --images --keep-latest-images 1 --write --vacuum

# Browse and curate interactively.
zcm tui
```

The database is found automatically at
`${XDG_DATA_HOME:-~/.local/share}/zed/threads/threads.db`. Override it any time
with `--db /path/to/threads.db`.

---

## How Zed stores threads

Everything lives in one SQLite table, `threads`:

| column               | meaning                                            |
| -------------------- | -------------------------------------------------- |
| `id`                 | UUID of the thread                                 |
| `summary`            | the thread title shown in Zed                      |
| `updated_at`         | last-modified timestamp (ISO 8601)                 |
| `created_at`         | creation timestamp                                 |
| `data_type`          | payload encoding (`zstd` or `json`)                |
| `data`               | the thread document (zstd-compressed JSON)         |
| `parent_id`          | parent thread, if any                              |
| `folder_paths`       | project folder(s), newline-separated               |
| `folder_paths_order` | display order of those folders                     |

The decoded document contains the message list plus metadata such as the
**next model** to use (`{"provider": ..., "model": ...}`) and the agent
`profile`. This tool understands the document well enough to edit messages and
metadata while leaving everything else untouched.

---

## Concepts: threads, messages, parts

- A **thread** is one conversation.
- A thread contains ordered **messages**, each with a `role` (`user` or
  `assistant`).
- A message contains ordered **parts**. A part is the smallest thing you can
  select, drop, or edit:

| part type     | what it is                                              |
| ------------- | ------------------------------------------------------- |
| `text`        | normal user or assistant text                           |
| `thinking`    | assistant reasoning / thinking blocks                   |
| `image`       | an embedded image                                       |
| `tool_use`    | a tool call the agent made (e.g. `read_file`, `terminal`) |
| `tool_result` | the output returned to the agent (often the bulky bit)  |
| `mention`     | a context mention (e.g. another thread, a file)         |

Every part has a stable id shown as `message:slot:index` (for example
`21:tool_result:0`). Tool parts also expose a **target** -- the file path,
glob, URL, command, or query pulled from the tool's input -- which is what the
path/regex filters match against.

---

## Command-line interface

```text
zcm [--db PATH] [-s] <command> [options]
```

Run `zcm <command> --help` for the full, authoritative option list.

### Global options

| option              | description                                              |
| ------------------- | -------------------------------------------------------- |
| `--db PATH`         | Path to `threads.db` (default: Zed's data directory).    |
| `-s, --case-sensitive` | Make all matching case-sensitive (default: insensitive). |
| `--version`         | Print the version and exit.                              |

### `list`

List threads that match the [thread filters](#thread-filters).

```sh
zcm list                              # newest first
zcm list --project m3u8-extractor     # only one project
zcm list --sort size --count-messages # biggest first, show message counts
zcm list --min-thread-size 1M          # only threads at least 1 MiB on disk
zcm list --format ids                 # just ids (handy for scripting)
zcm list --format json                # machine-readable
```

### `show`

Print one thread's messages and parts, with part ids, sizes, tool names, and
targets. Accepts a full or **prefix** thread id, or any thread filter (shows
the first match).

```sh
zcm show 6395f4e5                     # by id prefix
zcm show --title-contains "Signal"    # by title
zcm show 6395f4e5 --type image        # only image parts
zcm show 6395f4e5 --sort size         # biggest parts first
zcm show 6395f4e5 --message 21 --full # one message, full text
```

Use `--sort {document,size,kind}` (with optional `--reverse`) to reorder the
parts -- `--sort size` is the quickest way to find what is bloating a thread.

### `search`

Search titles and part text across all matching threads; matches are
highlighted.

```sh
zcm search "permission denied"
zcm search "fatal: .*not a git repo" --regex
zcm search TODO --type text --role assistant --max-per-thread 3
```

### `stats`

Aggregate overview: thread/message/image counts, plus top projects, models,
tools, and part types. Honors thread filters.

```sh
zcm stats
zcm stats --project portage
```

### `drop`

Drop or prune matching parts. **Dry run unless `--write`.** Requires at least
one selector (a part filter, a position selector, `--images`, or
`--keep-latest-images`) unless you pass `--all`.

```sh
# Replace every terminal result with a placeholder in one thread.
zcm drop --thread-id <id> --type tool_result --tool terminal

# Drop all reads of PNG files anywhere.
zcm drop --tool read_file --path-glob '**/*.png' --write

# Strip images but keep the newest one in each thread.
zcm drop --images --keep-latest-images 1 --write --vacuum

# Remove (not just placeholder) every thinking block.
zcm drop --type thinking --mode remove --write
```

| option                    | description                                              |
| ------------------------- | -------------------------------------------------------- |
| `--mode {placeholder,remove,strip-images}` | how to drop (default `placeholder`).      |
| `--images`                | preset: target image parts and strip them.               |
| `--keep-latest-images N`  | keep the newest *N* image-bearing parts per thread.      |
| `--invert`                | act on parts that do **not** match the filter.           |
| `--all`                   | allow dropping with no filter (dangerous).               |

### `edit`

Rewrite part text in bulk. Use a regex replacement or set the text literally.

```sh
# Redact a token everywhere it appears in text/results.
zcm edit --part-regex "ghp_[A-Za-z0-9]+" --replace "ghp_[A-Za-z0-9]+" \
    --with "<redacted>" --write

# Replace one part's entire text.
zcm edit --thread-id <id> --part-contains "old note" --set-text "new note" --write
```

### `thread`

Whole-thread operations. Combine with thread filters to act in bulk.

```sh
# Change which model will run next.
zcm thread --thread-id <id> --set-model "zed.dev:gpt-5.5" --write

# Move every thread from one project to another.
zcm thread --project old-name --reassign /home/me/new-project --write

# Rename and re-profile.
zcm thread --thread-id <id> --set-title "Cleanup notes" --set-profile ask --write

# Delete matching threads (prompts unless -y).
zcm thread --title-contains "New Thread" --max-messages 0 --delete --write -y
```

`--set-model` takes `PROVIDER:MODEL`, e.g. `zed.dev:claude-opus-4-8`,
`openai:gpt-5.5`, or `Local Proxy:unsloth/GLM-5`.

### `backup`

Write a timestamped copy next to the database and exit.

```sh
zcm backup
```

### `tui`

Launch the [interactive UI](#tui-guide). Thread filters pre-narrow the list.

```sh
zcm tui
zcm tui --project portage
zcm tui --read-only
```

---

## Filter reference

### Thread filters

| option | matches |
| ------ | ------- |
| `--thread-id ID` | exact id (repeatable, comma-separated) |
| `--title-contains` / `--title-regex` | the title |
| `--content-contains` / `--content-regex` | title **and** all message text |
| `--folder` / `--folder-glob` / `--project` | project folder path / glob / basename |
| `--model` / `--model-regex` | next model, as `provider/model` |
| `--profile` | agent profile |
| `--parent-id` | parent thread id |
| `--created-after` / `--created-before` | creation date (ISO 8601) |
| `--updated-after` / `--updated-before` | update date |
| `--has-images` / `--no-images` | presence of images |
| `--min-messages` / `--max-messages` | message count |
| `--min-thread-size` / `--max-thread-size` | stored thread size (accepts `K`/`M`/`G`) |
| `--limit N` | stop after N matching threads |

### Part filters

| option | matches |
| ------ | ------- |
| `--role {user,assistant}` | message role |
| `--type {text,thinking,image,tool_use,tool_result,mention,other}` | part type |
| `--tool NAME` | tool name (repeatable, comma-separated) |
| `--path-glob GLOB` | tool target path (glob; repeatable) |
| `--path-regex` / `--target-regex` | tool target path / any target (regex) |
| `--part-contains` / `--part-regex` | the part's text |
| `--errors-only` / `--no-errors` | tool-result error status |
| `--images-only` | parts that carry image data |
| `--min-size` / `--max-size` | serialized part size (accepts `K`/`M`/`G`) |

Most commands also sort by size: `list --sort size`, `show --sort size`.

### Position selectors (within each thread)

| option | selects |
| ------ | ------- |
| `--oldest N` | the first N messages |
| `--newest N` | the last N messages |
| `--index-min` / `--index-max` | messages in an index range |
| `--middle` | the middle, with `--keep-oldest`/`--keep-newest` protecting the ends |

Dates apply at the **thread** level (Zed does not timestamp individual
messages); positions apply **within** a thread.

---

## Recipes

**Reclaim space from image-heavy threads (the classic use case).**

```sh
zcm drop --images --keep-latest-images 1 --write --vacuum
```

**Drop only the reads of a noisy directory, everywhere.**

```sh
zcm drop --tool read_file --path-glob '**/node_modules/**' --write
```

**Trim long sessions: keep the last 10 messages, placeholder the rest.**

```sh
zcm drop --middle --keep-newest 10 --type tool_result --type tool_use --write
```

**Forget everything before a date.**

```sh
zcm thread --updated-before 2025-01-01 --delete --write -y
```

**Find what is bloating a thread, then prune the biggest parts.**

```sh
zcm list --min-thread-size 1M --sort size   # heaviest threads
zcm show <id> --sort size                   # heaviest parts in one thread
zcm drop --thread-id <id> --min-size 50K --write
```

**Bulk-redact secrets that leaked into results.**

```sh
zcm edit --type tool_result --replace "AKIA[0-9A-Z]{16}" --with "<aws-key>" --write
```

**Point a batch of threads at a different next model.**

```sh
zcm thread --model "claude-opus-4-6" --set-model "zed.dev:claude-opus-4-8" --write
```

---

## TUI guide

Run `zcm tui`. All changes are **staged in memory** and only written when you
press `w`, which always makes a backup first.

**Thread list**

| key | action |
| --- | ------ |
| `j` / `k`, arrows | move |
| PgUp / PgDn, `g` / `G` | page / jump to ends |
| Enter / `l` | open thread |
| `/` | search (title, project, content) |
| `s` | cycle sort (updated / size / title) |
| `d` | toggle delete mark |
| `r` | reassign project folder |
| `m` | change next model (`provider:model`) |
| `t` | change title |
| `w` | write staged changes |
| `?` | help |
| `q` | quit |

**Thread detail**

| key | action |
| --- | ------ |
| `j` / `k`, arrows | move |
| `space` | select / deselect a part |
| `a` / `c` | select all / clear selection |
| `d` | stage drop (placeholder) |
| `D` | stage remove (delete the part) |
| `i` | stage strip-images |
| `e` | edit text inline |
| `E` | edit text in `$EDITOR` |
| `u` | clear staged change |
| `/` | find within the thread |
| `s` | toggle size-sorted (largest first) view |
| Enter | view full part text |
| `w` | write staged changes |
| `q` / `h` / Esc | back to the list |

---

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"

ruff check .
ruff format .
```

CI runs [Super-Linter](https://github.com/super-linter/super-linter) (Ruff for
Python) on every push and pull request, and builds/publishes the package on
tagged releases. Dependencies are kept current with Dependabot.

When testing changes, always point `--db` at a **copy** of a real database and
rely on the default dry-run output before re-running with `--write`.

---

## License

Licensed under the GNU Affero General Public License v3.0 only
(`AGPL-3.0-only`). See [LICENSE](LICENSE).
