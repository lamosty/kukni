#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Render installer metadata without interpreting path text as shell syntax."""

from __future__ import annotations

from pathlib import Path
import sys


TOKEN = "@KUKNI_EXEC@"


def quote_exec_path(path: str, *, desktop: bool) -> str:
    """Quote one executable path for Desktop Entry or D-Bus Exec parsing."""

    # The installer rejects the characters whose escaping rules differ between
    # Desktop Entry and D-Bus service syntax. A quoted path can then be shared;
    # Desktop Entry alone expands percent field codes, so literal percents double.
    if any(character in path for character in ('\\', '"', "`", "$")):
        raise ValueError("executable path contains metadata-hostile characters")
    escaped = path
    if desktop:
        escaped = escaped.replace("%", "%%")
    return f'"{escaped}"'


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in {"desktop", "dbus"}:
        print(
            "usage: render-template.py {desktop|dbus} EXECUTABLE TEMPLATE",
            file=sys.stderr,
        )
        return 2

    kind, executable, template_name = argv[1:]
    if any(character in executable for character in ("\0", "\n", "\r")):
        print("executable path contains an unsupported control character", file=sys.stderr)
        return 1

    template = Path(template_name).read_text(encoding="utf-8")
    if template.count(TOKEN) != 1:
        print("template must contain exactly one executable token", file=sys.stderr)
        return 1

    try:
        quoted_executable = quote_exec_path(executable, desktop=kind == "desktop")
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    rendered = template.replace(TOKEN, quoted_executable)
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
