#!/bin/sh
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

set -eu

force=0

usage() {
    printf 'Usage: %s [--force]\n' "$0"
    printf 'Remove Kukni from the current user account.\n'
}

for argument in "$@"; do
    case "$argument" in
        --force)
            force=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then
    printf 'Do not run this uninstaller as root or with sudo.\n' >&2
    exit 1
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
data_home=${XDG_DATA_HOME:-"${HOME:?HOME is not set}/.local/share"}
case "$data_home" in
    /*) ;;
    *)
        printf 'XDG_DATA_HOME must be an absolute path.\n' >&2
        exit 1
        ;;
esac
if [ "$data_home" = / ]; then
    printf 'Refusing to uninstall directly below the filesystem root.\n' >&2
    exit 1
fi

sushi_dir=$data_home/sushi
viewer_dir=$sushi_dir/viewers
helper_dir=$sushi_dir/helpers
viewer_source=$script_dir/viewers/kukni.js
helper_source=$script_dir/helpers/kukni-extract-preview.py
viewer_target=$viewer_dir/kukni.js
helper_target=$helper_dir/kukni-extract-preview.py

check_removal() {
    source_file=$1
    target_file=$2

    if [ ! -e "$target_file" ] && [ ! -L "$target_file" ]; then
        return
    fi
    if [ -f "$target_file" ] && cmp -s -- "$source_file" "$target_file"; then
        return
    fi
    if [ "$force" -ne 1 ]; then
        printf 'Refusing to remove modified or unexpected file: %s\n' "$target_file" >&2
        printf 'Review it first, then rerun with --force if removal is intended.\n' >&2
        exit 1
    fi
}

check_removal "$viewer_source" "$viewer_target"
check_removal "$helper_source" "$helper_target"

rm -f -- "$viewer_target" "$helper_target"
rmdir -- "$viewer_dir" 2>/dev/null || true
rmdir -- "$helper_dir" 2>/dev/null || true
rmdir -- "$sushi_dir" 2>/dev/null || true

printf 'Removed Kukni from the current user account.\n'
printf 'If Sushi is running, reload it with: pkill -x sushi\n'
