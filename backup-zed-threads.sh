#!/bin/sh

DB=${XDG_DATA_HOME:-~/.local/share}/zed/threads/threads.db
cp "$DB" "$DB.bak.$(date +%Y%m%d-%H%M%S)"
