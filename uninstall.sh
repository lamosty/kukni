#!/bin/sh
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

set -eu

force=0

usage() {
    printf 'Usage: %s [--force]\n' "$0"
    printf 'Remove the standalone Kukni application from the current user account.\n'
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

for required_command in id dirname realpath readlink sha256sum stat mktemp awk \
    grep rm rmdir mkdir; do
    if ! command -v "$required_command" >/dev/null 2>&1; then
        printf 'Required uninstall command is unavailable: %s\n' "$required_command" >&2
        exit 1
    fi
done

if [ "$(id -u)" -eq 0 ] || [ -n "${SUDO_USER:-}" ] || [ -n "${SUDO_UID:-}" ]; then
    printf 'Do not run this uninstaller as root or with sudo.\n' >&2
    exit 1
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
home=${HOME:?HOME is not set}
tab=$(printf '\t')
newline=$(printf '\nx')
newline=${newline%x}
carriage_return=$(printf '\rx')
carriage_return=${carriage_return%x}
backslash='\'
double_quote='"'
backtick='`'
dollar='$'

validate_base_path() {
    base_label=$1
    base_value=$2

    case "$base_value" in
        /*) ;;
        *)
            printf '%s must be an absolute path: %s\n' "$base_label" "$base_value" >&2
            exit 1
            ;;
    esac
    case "$base_value" in
        *"$tab"*|*"$newline"*|*"$carriage_return"*)
            printf '%s contains an unsupported control character.\n' "$base_label" >&2
            exit 1
            ;;
    esac
    case "$base_value" in
        *"$backslash"*|*"$double_quote"*|*"$backtick"*|*"$dollar"*)
            printf '%s cannot contain backslash, double quote, backtick, or dollar characters.\n' \
                "$base_label" >&2
            exit 1
            ;;
    esac

    base_value=$(realpath -m -- "$base_value")
    case "$base_value" in
        *"$backslash"*|*"$double_quote"*|*"$backtick"*|*"$dollar"*)
            printf '%s resolves to a path that desktop and D-Bus metadata cannot encode portably.\n' \
                "$base_label" >&2
            exit 1
            ;;
    esac
    case "$base_value" in
        /|/bin|/bin/*|/boot|/boot/*|/dev|/dev/*|/etc|/etc/*|/lib|/lib/*|/lib32|/lib32/*|/lib64|/lib64/*|/opt|/opt/*|/proc|/proc/*|/root|/root/*|/run|/run/*|/sbin|/sbin/*|/srv|/srv/*|/sys|/sys/*|/usr|/usr/*|/var|/var/*)
            printf 'Refusing to uninstall from system location %s.\n' "$base_value" >&2
            exit 1
            ;;
    esac
    printf '%s\n' "$base_value"
}

if [ "${PREFIX+x}" = x ]; then
    prefix=$PREFIX
elif [ -f "$script_dir/.install-manifest" ] && [ ! -L "$script_dir/.install-manifest" ]; then
    # An installed copy lives at PREFIX/lib/kukni/uninstall.sh, allowing a
    # custom PREFIX installation to remove itself without extra environment.
    prefix=$(dirname -- "$(dirname -- "$script_dir")")
else
    prefix=$home/.local
fi
prefix=$(validate_base_path PREFIX "$prefix")
app_root=$prefix/lib/kukni
manifest=$app_root/.install-manifest

if [ ! -e "$manifest" ] && [ ! -L "$manifest" ]; then
    default_data=${XDG_DATA_HOME:-"$prefix/share"}
    default_data=$(validate_base_path XDG_DATA_HOME "$default_data")
    if [ ! -e "$prefix/bin/kukni" ] && [ ! -L "$prefix/bin/kukni" ] && \
        [ ! -e "$app_root" ] && [ ! -L "$app_root" ] && \
        [ ! -e "$default_data/applications/io.github.lamosty.Kukni.desktop" ] && \
        [ ! -L "$default_data/applications/io.github.lamosty.Kukni.desktop" ] && \
        [ ! -e "$default_data/dbus-1/services/io.github.lamosty.Kukni.service" ] && \
        [ ! -L "$default_data/dbus-1/services/io.github.lamosty.Kukni.service" ] && \
        [ ! -e "$default_data/dbus-1/services/org.gnome.NautilusPreviewer.service" ] && \
        [ ! -L "$default_data/dbus-1/services/org.gnome.NautilusPreviewer.service" ]; then
        printf 'Kukni is not installed for this prefix.\n'
        exit 0
    fi
    printf 'Refusing to remove files without Kukni ownership manifest: %s\n' \
        "$manifest" >&2
    printf 'Review the remaining files and remove them manually.\n' >&2
    exit 1
fi

if [ ! -f "$manifest" ] || [ -L "$manifest" ]; then
    printf 'Refusing to use an unexpected Kukni ownership manifest: %s\n' "$manifest" >&2
    exit 1
fi

manifest_version=$(awk -F '\t' '$1 == "version" { print $2 }' "$manifest")
manifest_prefix=$(awk -F '\t' '$1 == "prefix" { print $2 }' "$manifest")
manifest_data=$(awk -F '\t' '$1 == "data" { print $2 }' "$manifest")
if [ "$manifest_version" != 1 ] || [ -z "$manifest_prefix" ] || [ -z "$manifest_data" ]; then
    printf 'Refusing to use a malformed Kukni ownership manifest: %s\n' "$manifest" >&2
    exit 1
fi
manifest_prefix=$(validate_base_path 'manifest PREFIX' "$manifest_prefix")
data_home=$(validate_base_path 'manifest XDG_DATA_HOME' "$manifest_data")
if [ "$manifest_prefix" != "$prefix" ]; then
    printf 'Kukni ownership manifest belongs to another prefix: %s\n' \
        "$manifest_prefix" >&2
    exit 1
fi

validate_relative_path() {
    relative_value=$1
    case "$relative_value" in
        ''|/*|*"$tab"*|*"$newline"*|*"$carriage_return"*|*//*) return 1 ;;
    esac
    case "/$relative_value/" in
        */../*|*/./*) return 1 ;;
    esac
    return 0
}

record_is_allowed() {
    record_root=$1
    record_relative=$2
    validate_relative_path "$record_relative" || return 1
    case "$record_root:$record_relative" in
        P:bin/kukni|P:lib/kukni/*)
            return 0
            ;;
        D:applications/io.github.lamosty.Kukni.desktop|D:dbus-1/services/io.github.lamosty.Kukni.service|D:dbus-1/services/org.gnome.NautilusPreviewer.service)
            return 0
            ;;
    esac
    return 1
}

removal_root=$(mktemp -d "${TMPDIR:-/tmp}/kukni-uninstall.XXXXXX")
removal_plan=$removal_root/plan
seen_records=$removal_root/seen
: > "$removal_plan"
: > "$seen_records"

cleanup() {
    cleanup_status=$?
    trap - EXIT HUP INT TERM
    if [ -d "$removal_root" ] && [ "$removal_root" != / ]; then
        rm -rf -- "$removal_root"
    fi
    exit "$cleanup_status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

manifest_error=0
record_count=0
while IFS="$tab" read -r record_kind field_two field_three field_four field_five extra_field; do
    case "$record_kind" in
        version|prefix|data)
            # Headers were checked above; extra fields would make the file ambiguous.
            if [ -n "$field_three" ] || [ -n "$field_four" ] || \
                [ -n "$field_five" ] || [ -n "$extra_field" ]; then
                manifest_error=1
                break
            fi
            ;;
        file|link)
            installed_kind=$record_kind
            record_hash=$field_two
            record_mode=$field_three
            record_root=$field_four
            record_relative=$field_five
            if [ -n "$extra_field" ] || [ "${#record_hash}" -ne 64 ]; then
                manifest_error=1
                break
            fi
            case "$record_hash" in
                *[!0-9a-f]*) manifest_error=1; break ;;
            esac
            case "$record_mode" in
                644|755|777) ;;
                *) manifest_error=1; break ;;
            esac
            if { [ "$installed_kind" = file ] && [ "$record_mode" = 777 ]; } || \
                { [ "$installed_kind" = link ] && [ "$record_mode" != 777 ]; }; then
                manifest_error=1
                break
            fi
            if ! record_is_allowed "$record_root" "$record_relative"; then
                manifest_error=1
                break
            fi
            record_key=$record_root$tab$record_relative
            if grep -Fqx -- "$record_key" "$seen_records"; then
                manifest_error=1
                break
            fi
            printf '%s\n' "$record_key" >> "$seen_records"

            case "$record_root" in
                P) record_base=$prefix ;;
                D) record_base=$data_home ;;
                *) manifest_error=1; break ;;
            esac
            record_target=$record_base/$record_relative
            resolved_parent=$(realpath -m -- "$(dirname -- "$record_target")")
            case "$resolved_parent" in
                "$record_base"/*) ;;
                *) manifest_error=1; break ;;
            esac
            printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
                "$installed_kind" "$record_hash" "$record_mode" "$record_root" \
                "$record_relative" "$record_target" >> "$removal_plan"
            record_count=$((record_count + 1))
            ;;
        '')
            manifest_error=1
            break
            ;;
        *)
            manifest_error=1
            break
            ;;
    esac
done < "$manifest"

if [ "$manifest_error" -ne 0 ] || [ "$record_count" -eq 0 ]; then
    printf 'Refusing to use a malformed Kukni ownership manifest: %s\n' "$manifest" >&2
    exit 1
fi

while IFS="$tab" read -r installed_kind expected_hash expected_mode _record_root \
    _record_relative target_file; do
    if [ -d "$target_file" ] && [ ! -L "$target_file" ]; then
        printf 'Refusing to remove unexpected directory: %s\n' "$target_file" >&2
        exit 1
    fi
    if [ ! -e "$target_file" ] && [ ! -L "$target_file" ]; then
        continue
    fi
    if [ "$installed_kind" = link ] && [ -L "$target_file" ]; then
        current_hash=$(readlink -n -- "$target_file" | \
            sha256sum | awk '{ print $1 }')
        if [ "$current_hash" = "$expected_hash" ]; then
            continue
        fi
    elif [ "$installed_kind" = file ] && [ -f "$target_file" ] && \
        [ ! -L "$target_file" ]; then
        current_hash=$(sha256sum -- "$target_file" | awk '{ print $1 }')
        current_mode=$(stat -c '%a' -- "$target_file")
        if [ "$current_hash" = "$expected_hash" ] && [ "$current_mode" = "$expected_mode" ]; then
            continue
        fi
    fi
    if [ "$force" -ne 1 ]; then
        printf 'Refusing to remove modified or unexpected file: %s\n' "$target_file" >&2
        printf 'Review it first, then rerun with --force if removal is intended.\n' >&2
        exit 1
    fi
done < "$removal_plan"

while IFS="$tab" read -r _installed_kind _expected_hash _expected_mode _record_root \
    _record_relative target_file; do
    rm -f -- "$target_file"
done < "$removal_plan"
rm -f -- "$manifest"

# Remove only the private application directories and only while they are empty.
rmdir -- "$app_root/src/kukni/renderers" 2>/dev/null || true
rmdir -- "$app_root/src/kukni" 2>/dev/null || true
rmdir -- "$app_root/src" 2>/dev/null || true
rmdir -- "$app_root/helpers" 2>/dev/null || true
rmdir -- "$app_root/launcher" 2>/dev/null || true
rmdir -- "$app_root/bin" 2>/dev/null || true
rmdir -- "$app_root" 2>/dev/null || true

# Session buses cache activation metadata. Reloading is best effort because the
# application files are already removed and headless sessions may have no bus.
if [ "${KUKNI_SKIP_DBUS_RELOAD:-0}" != 1 ] && \
    command -v dbus-send >/dev/null 2>&1; then
    dbus-send --session --type=method_call \
        --dest=org.freedesktop.DBus /org/freedesktop/DBus \
        org.freedesktop.DBus.ReloadConfig >/dev/null 2>&1 || true
fi

printf 'Removed standalone Kukni from the current user account.\n'
if [ -d "$app_root" ]; then
    printf 'Unowned files remain in: %s\n' "$app_root"
fi
printf 'A running previewer is unaffected; sign out and back in to restore the default previewer.\n'
