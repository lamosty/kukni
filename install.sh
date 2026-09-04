#!/bin/sh
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

set -eu

force=0

usage() {
    printf 'Usage: %s [--force]\n' "$0"
    printf 'Install Kukni for the current user.\n'
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
    printf 'Do not run this installer as root or with sudo.\n' >&2
    exit 1
fi

if ! command -v sushi >/dev/null 2>&1; then
    printf 'GNOME Sushi is not installed. Install it with your distribution package manager.\n' >&2
    printf 'The package is named gnome-sushi on Debian/Ubuntu and sushi on many other distributions.\n' >&2
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
    printf 'Refusing to install directly below the filesystem root.\n' >&2
    exit 1
fi

sushi_dir=$data_home/sushi
viewer_dir=$sushi_dir/viewers
helper_dir=$sushi_dir/helpers
viewer_source=$script_dir/viewers/kukni.js
helper_source=$script_dir/helpers/kukni-extract-preview.py
viewer_target=$viewer_dir/kukni.js
helper_target=$helper_dir/kukni-extract-preview.py

check_target() {
    source_file=$1
    target_file=$2

    if [ -d "$target_file" ] && [ ! -L "$target_file" ]; then
        printf 'Refusing to replace directory: %s\n' "$target_file" >&2
        exit 1
    fi
    if [ -e "$target_file" ] || [ -L "$target_file" ]; then
        if cmp -s -- "$source_file" "$target_file"; then
            return
        fi
        if [ "$force" -ne 1 ]; then
            printf 'Refusing to overwrite modified file: %s\n' "$target_file" >&2
            printf 'Review it first, then rerun with --force if replacement is intended.\n' >&2
            exit 1
        fi
    fi
}

check_target "$viewer_source" "$viewer_target"
check_target "$helper_source" "$helper_target"

install -d -m 0755 -- "$viewer_dir" "$helper_dir"
viewer_temp=$(mktemp "$viewer_dir/.kukni.js.XXXXXX")
helper_temp=$(mktemp "$helper_dir/.kukni-extract-preview.py.XXXXXX")

cleanup() {
    if [ -n "${viewer_temp:-}" ]; then
        rm -f -- "$viewer_temp"
    fi
    if [ -n "${helper_temp:-}" ]; then
        rm -f -- "$helper_temp"
    fi
}
trap cleanup EXIT HUP INT TERM

install -m 0644 -- "$viewer_source" "$viewer_temp"
install -m 0755 -- "$helper_source" "$helper_temp"
mv -f -- "$helper_temp" "$helper_target"
helper_temp=
mv -f -- "$viewer_temp" "$viewer_target"
viewer_temp=

printf 'Installed Kukni for the current user.\n'
printf 'Close any open preview, then select a CR2 in Files and press Space.\n'
printf 'If Sushi was already running, reload it with: pkill -x sushi\n'
