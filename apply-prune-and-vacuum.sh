#!/bin/sh

DB=${XDG_DATA_HOME:-~/.local/share}/zed/threads/threads.db
python3 prune_zed_read_file_images.py "$DB" --title-contains "Stabilize Cactus GIFs for Signal Stickers" --write --vacuum
