#!/bin/sh
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
unset WAYLAND_DISPLAY
# Isolate settings, display, and D-Bus together. This default private bus has no
# host service directories, accessibility bus, or desktop portal activation.
# The explicit marker lets the installed-activation test refuse a plain launch;
# that test uses a second private bus containing only packaged Kukni services.
exec xvfb-run -a -s '-screen 0 1920x1080x24' \
    dbus-run-session --config-file="$project_dir/tests/session-bus.conf" -- \
    env GDK_BACKEND=x11 GSK_RENDERER=cairo GSETTINGS_BACKEND=memory \
    GIO_USE_VFS=local GTK_A11Y=none KUKNI_ISOLATED_UI_TEST=1 "$@"
