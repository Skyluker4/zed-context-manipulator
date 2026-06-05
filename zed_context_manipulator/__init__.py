"""Zed Context Manipulator.

A toolkit for searching, filtering, editing, and pruning the conversation
threads that the Zed editor's agent stores in its local ``threads.db``
SQLite database.

The package exposes both a command-line interface (see :mod:`zed_context_manipulator.cli`)
and a curses-based terminal user interface (see :mod:`zed_context_manipulator.tui`).
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
