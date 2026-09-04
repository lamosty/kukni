#!/bin/sh
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test_root=$(mktemp -d "${TMPDIR:-/tmp}/kukni-test.XXXXXX")

cleanup() {
    if [ -n "${test_root:-}" ] && [ "$test_root" != / ]; then
        rm -rf -- "$test_root"
    fi
}
trap cleanup EXIT HUP INT TERM

export XDG_DATA_HOME=$test_root/data

"$project_dir/install.sh"
cmp -- "$project_dir/viewers/kukni.js" "$XDG_DATA_HOME/sushi/viewers/kukni.js"
cmp -- "$project_dir/helpers/kukni-extract-preview.py" \
    "$XDG_DATA_HOME/sushi/helpers/kukni-extract-preview.py"

viewer_mode=$(stat -c '%a' "$XDG_DATA_HOME/sushi/viewers/kukni.js")
helper_mode=$(stat -c '%a' "$XDG_DATA_HOME/sushi/helpers/kukni-extract-preview.py")
test "$viewer_mode" = 644
test "$helper_mode" = 755

printf '\n// local change\n' >> "$XDG_DATA_HOME/sushi/viewers/kukni.js"
if "$project_dir/install.sh" >/dev/null 2>&1; then
    printf 'installer unexpectedly overwrote a modified file\n' >&2
    exit 1
fi

"$project_dir/install.sh" --force
"$project_dir/uninstall.sh"
test ! -e "$XDG_DATA_HOME/sushi/viewers/kukni.js"
test ! -e "$XDG_DATA_HOME/sushi/helpers/kukni-extract-preview.py"

printf 'install/uninstall tests passed\n'
