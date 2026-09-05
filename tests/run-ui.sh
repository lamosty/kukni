#!/bin/sh
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
unset WAYLAND_DISPLAY
# Isolate settings, display, and D-Bus together; never activate the installed
# Kukni, the user's accessibility bus, or desktop portals during smoke tests.
exec xvfb-run -a -s '-screen 0 1920x1080x24' \
    dbus-run-session --config-file="$project_dir/tests/session-bus.conf" -- \
    env GDK_BACKEND=x11 GSK_RENDERER=cairo GSETTINGS_BACKEND=memory \
    GIO_USE_VFS=local GTK_A11Y=none "$@"
