#!/bin/sh
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

set -eu

force=0

usage() {
    printf 'Usage: %s [--force]\n' "$0"
    printf 'Install the standalone Kukni application for the current user.\n'
    printf 'PREFIX defaults to $HOME/.local; XDG_DATA_HOME defaults to $PREFIX/share.\n'
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

for required_command in id dirname realpath readlink python3 sha256sum install \
    stat mktemp awk find sort cp mv ln chmod cut uniq cmp grep mkdir rm rmdir; do
    if ! command -v "$required_command" >/dev/null 2>&1; then
        printf 'Required installation command is unavailable: %s\n' "$required_command" >&2
        exit 1
    fi
done

if [ "$(id -u)" -eq 0 ] || [ -n "${SUDO_USER:-}" ] || [ -n "${SUDO_UID:-}" ]; then
    printf 'Do not run this installer as root or with sudo.\n' >&2
    exit 1
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
home=${HOME:?HOME is not set}
prefix=${PREFIX:-"$home/.local"}
data_home=${XDG_DATA_HOME:-"$prefix/share"}
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
            printf 'Choose another path so desktop and D-Bus activation metadata stays portable.\n' >&2
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
            printf 'Refusing to install into system location %s.\n' "$base_value" >&2
            exit 1
            ;;
    esac
    printf '%s\n' "$base_value"
}

prefix=$(validate_base_path PREFIX "$prefix")
data_home=$(validate_base_path XDG_DATA_HOME "$data_home")
app_root=$prefix/lib/kukni
manifest_target=$app_root/.install-manifest
launcher_target=$prefix/bin/kukni
launcher_link=../lib/kukni/launcher/kukni
python_runtime=/usr/bin/python3

if [ ! -x "$python_runtime" ] || ! "$python_runtime" -B -c '
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk
' >/dev/null 2>&1; then
    printf 'Kukni requires Python GObject introspection with GTK 4 and libadwaita 1.\n' >&2
    printf 'Install those runtime bindings with your distribution package manager, then retry.\n' >&2
    exit 1
fi
if [ ! -x /usr/bin/prlimit ] && [ ! -x /bin/prlimit ]; then
    printf 'Kukni CR2 previews require prlimit from the util-linux package.\n' >&2
    printf 'Install that runtime with your distribution package manager, then retry.\n' >&2
    exit 1
fi

for required_source in \
    "$script_dir/bin/kukni" \
    "$script_dir/src/kukni/__init__.py" \
    "$script_dir/helpers/kukni-cr2-worker.py" \
    "$script_dir/helpers/kukni-image-worker.py" \
    "$script_dir/helpers/kukni-extract-preview.py" \
    "$script_dir/helpers/kukni-media-worker.py" \
    "$script_dir/uninstall.sh" \
    "$script_dir/packaging/kukni-launcher" \
    "$script_dir/packaging/render-template.py" \
    "$script_dir/packaging/io.github.lamosty.Kukni.desktop.in" \
    "$script_dir/packaging/io.github.lamosty.Kukni.service.in" \
    "$script_dir/packaging/org.gnome.NautilusPreviewer.service.in"; do
    if [ ! -f "$required_source" ] || [ -L "$required_source" ]; then
        printf 'Required source file is missing or unexpected: %s\n' "$required_source" >&2
        exit 1
    fi
done

stage_root=$(mktemp -d "${TMPDIR:-/tmp}/kukni-install.XXXXXX")
plan_file=$stage_root/plan
applied_file=$stage_root/applied
created_dirs_file=$stage_root/created-dirs
: > "$plan_file"
: > "$applied_file"
: > "$created_dirs_file"
transaction_committed=0
current_temp=

rollback_installation() {
    if [ -s "$applied_file" ]; then
        awk '{ lines[NR] = $0 } END { for (line = NR; line > 0; line--) print lines[line] }' \
            "$applied_file" |
        while IFS="$tab" read -r rollback_target rollback_backup; do
            [ -n "$rollback_target" ] || continue
            if [ "$rollback_backup" = - ]; then
                rm -f -- "$rollback_target"
            elif [ -e "$rollback_backup" ] || [ -L "$rollback_backup" ]; then
                rm -f -- "$rollback_target"
                mv -f -- "$rollback_backup" "$rollback_target"
            fi
        done
    fi
    if [ -s "$created_dirs_file" ]; then
        awk '{ lines[NR] = $0 } END { for (line = NR; line > 0; line--) print lines[line] }' \
            "$created_dirs_file" |
        while IFS= read -r rollback_directory; do
            rmdir -- "$rollback_directory" 2>/dev/null || true
        done
    fi
}

cleanup() {
    cleanup_status=$?
    trap - EXIT HUP INT TERM
    if [ "$transaction_committed" -ne 1 ]; then
        rollback_installation
    fi
    if [ -n "$current_temp" ]; then
        rm -f -- "$current_temp"
    fi
    if [ -d "$stage_root" ] && [ "$stage_root" != / ]; then
        rm -rf -- "$stage_root"
    fi
    exit "$cleanup_status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

validate_relative_path() {
    relative_value=$1
    case "$relative_value" in
        ''|/*|*"$tab"*|*"$newline"*|*"$carriage_return"*|*//*)
            printf 'Unsafe installation path: %s\n' "$relative_value" >&2
            exit 1
            ;;
    esac
    case "/$relative_value/" in
        */../*|*/./*)
            printf 'Unsafe installation path: %s\n' "$relative_value" >&2
            exit 1
            ;;
    esac
}

stage_source_file() {
    stage_source=$1
    stage_mode=$2
    stage_root_key=$3
    stage_relative=$4
    validate_relative_path "$stage_relative"

    stage_destination=$stage_root/$stage_root_key/$stage_relative
    install -d -m 0755 -- "$(dirname -- "$stage_destination")"
    install -m "$stage_mode" -- "$stage_source" "$stage_destination"
    printf '%s\t%s\t%s\n' "$stage_mode" "$stage_root_key" "$stage_relative" >> "$plan_file"
}

stage_source_file "$script_dir/packaging/kukni-launcher" 755 P \
    lib/kukni/launcher/kukni
stage_source_file "$script_dir/bin/kukni" 755 P lib/kukni/bin/kukni
stage_source_file "$script_dir/uninstall.sh" 755 P lib/kukni/uninstall.sh
stage_source_file "$script_dir/VERSION" 644 P lib/kukni/VERSION
stage_source_file "$script_dir/LICENSE" 644 P lib/kukni/LICENSE
stage_source_file "$script_dir/NOTICE.md" 644 P lib/kukni/NOTICE.md

find "$script_dir/src/kukni" -type f \
    \( -name '*.py' -o -name '*.css' \) \
    ! -path '*/__pycache__/*' -print | LC_ALL=C sort |
while IFS= read -r source_file; do
    relative_file=${source_file#"$script_dir/"}
    stage_source_file "$source_file" 644 P "lib/kukni/$relative_file"
done

find "$script_dir/helpers" -maxdepth 1 -type f -name '*.py' -print | LC_ALL=C sort |
while IFS= read -r source_file; do
    relative_file=${source_file#"$script_dir/"}
    stage_source_file "$source_file" 755 P "lib/kukni/$relative_file"
done

generated_dir=$stage_root/generated
install -d -m 0755 -- "$generated_dir"
"$python_runtime" -B "$script_dir/packaging/render-template.py" desktop "$launcher_target" \
    "$script_dir/packaging/io.github.lamosty.Kukni.desktop.in" \
    > "$generated_dir/io.github.lamosty.Kukni.desktop"
"$python_runtime" -B "$script_dir/packaging/render-template.py" dbus "$launcher_target" \
    "$script_dir/packaging/io.github.lamosty.Kukni.service.in" \
    > "$generated_dir/io.github.lamosty.Kukni.service"
"$python_runtime" -B "$script_dir/packaging/render-template.py" dbus "$launcher_target" \
    "$script_dir/packaging/org.gnome.NautilusPreviewer.service.in" \
    > "$generated_dir/org.gnome.NautilusPreviewer.service"
stage_source_file "$generated_dir/io.github.lamosty.Kukni.desktop" 644 D \
    applications/io.github.lamosty.Kukni.desktop
stage_source_file "$generated_dir/io.github.lamosty.Kukni.service" 644 D \
    dbus-1/services/io.github.lamosty.Kukni.service
stage_source_file "$generated_dir/org.gnome.NautilusPreviewer.service" 644 D \
    dbus-1/services/org.gnome.NautilusPreviewer.service

if duplicate_target=$(cut -f 2-3 "$plan_file" | LC_ALL=C sort | uniq -d) && \
    [ -n "$duplicate_target" ]; then
    printf 'Installer contains a duplicate target: %s\n' "$duplicate_target" >&2
    exit 1
fi

manifest_stage=$stage_root/manifest
{
    printf 'version\t1\n'
    printf 'prefix\t%s\n' "$prefix"
    printf 'data\t%s\n' "$data_home"
    while IFS="$tab" read -r file_mode root_key relative_file; do
        staged_file=$stage_root/$root_key/$relative_file
        file_hash=$(sha256sum -- "$staged_file" | awk '{ print $1 }')
        printf 'file\t%s\t%s\t%s\t%s\n' \
            "$file_hash" "$file_mode" "$root_key" "$relative_file"
    done < "$plan_file"
    launcher_link_hash=$(printf '%s' "$launcher_link" | sha256sum | awk '{ print $1 }')
    printf 'link\t%s\t777\tP\tbin/kukni\n' "$launcher_link_hash"
} > "$manifest_stage"
chmod 0644 "$manifest_stage"

old_manifest_valid=0
old_records_file=$stage_root/old-records
old_seen_file=$stage_root/old-seen
new_keys_file=$stage_root/new-keys
: > "$old_records_file"
: > "$old_seen_file"
while IFS="$tab" read -r _new_mode new_root new_relative; do
    printf '%s\t%s\n' "$new_root" "$new_relative" >> "$new_keys_file"
done < "$plan_file"
printf 'P\tbin/kukni\n' >> "$new_keys_file"

manifest_base_is_valid() {
    manifest_base=$1
    case "$manifest_base" in
        /*) ;;
        *) return 1 ;;
    esac
    case "$manifest_base" in
        *"$tab"*|*"$newline"*|*"$carriage_return"*|*"$backslash"*|*"$double_quote"*|*"$backtick"*|*"$dollar"*)
            return 1
            ;;
    esac
    resolved_manifest_base=$(realpath -m -- "$manifest_base")
    [ "$resolved_manifest_base" = "$manifest_base" ] || return 1
    case "$manifest_base" in
        /|/bin|/bin/*|/boot|/boot/*|/dev|/dev/*|/etc|/etc/*|/lib|/lib/*|/lib32|/lib32/*|/lib64|/lib64/*|/opt|/opt/*|/proc|/proc/*|/root|/root/*|/run|/run/*|/sbin|/sbin/*|/srv|/srv/*|/sys|/sys/*|/usr|/usr/*|/var|/var/*)
            return 1
            ;;
    esac
    return 0
}

manifest_relative_is_valid() {
    manifest_relative=$1
    case "$manifest_relative" in
        ''|/*|*"$tab"*|*"$newline"*|*"$carriage_return"*|*//*|*"$backslash"*)
            return 1
            ;;
    esac
    case "/$manifest_relative/" in
        */../*|*/./*) return 1 ;;
    esac
    return 0
}

manifest_record_is_allowed() {
    manifest_kind=$1
    manifest_mode=$2
    manifest_root=$3
    manifest_relative=$4
    manifest_relative_is_valid "$manifest_relative" || return 1
    case "$manifest_kind:$manifest_mode:$manifest_root:$manifest_relative" in
        link:777:P:bin/kukni)
            return 0
            ;;
        file:644:P:lib/kukni/*|file:755:P:lib/kukni/*|file:644:D:applications/io.github.lamosty.Kukni.desktop|file:644:D:dbus-1/services/io.github.lamosty.Kukni.service|file:644:D:dbus-1/services/org.gnome.NautilusPreviewer.service)
            return 0
            ;;
    esac
    return 1
}

if [ -e "$manifest_target" ] || [ -L "$manifest_target" ]; then
    old_manifest_format_valid=0
    if [ -f "$manifest_target" ] && [ ! -L "$manifest_target" ]; then
        old_version=$(awk -F '\t' '$1 == "version" { print $2 }' "$manifest_target")
        old_prefix=$(awk -F '\t' '$1 == "prefix" { print $2 }' "$manifest_target")
        old_data=$(awk -F '\t' '$1 == "data" { print $2 }' "$manifest_target")
        version_count=$(awk -F '\t' '$1 == "version" { count++ } END { print count + 0 }' "$manifest_target")
        prefix_count=$(awk -F '\t' '$1 == "prefix" { count++ } END { print count + 0 }' "$manifest_target")
        data_count=$(awk -F '\t' '$1 == "data" { count++ } END { print count + 0 }' "$manifest_target")
        old_manifest_error=0
        old_record_count=0

        if [ "$old_version" != 1 ] || [ "$version_count" -ne 1 ] || \
            [ "$prefix_count" -ne 1 ] || [ "$data_count" -ne 1 ] || \
            ! manifest_base_is_valid "$old_prefix" || \
            ! manifest_base_is_valid "$old_data"; then
            old_manifest_error=1
        else
            while IFS="$tab" read -r old_kind old_field_two old_field_three \
                old_field_four old_field_five old_extra; do
                case "$old_kind" in
                    version|prefix|data)
                        if [ -n "$old_field_three" ] || [ -n "$old_field_four" ] || \
                            [ -n "$old_field_five" ] || [ -n "$old_extra" ]; then
                            old_manifest_error=1
                            break
                        fi
                        ;;
                    file|link)
                        old_hash=$old_field_two
                        old_mode=$old_field_three
                        old_root=$old_field_four
                        old_relative=$old_field_five
                        if [ -n "$old_extra" ] || [ "${#old_hash}" -ne 64 ]; then
                            old_manifest_error=1
                            break
                        fi
                        case "$old_hash" in
                            *[!0-9a-f]*) old_manifest_error=1; break ;;
                        esac
                        if ! manifest_record_is_allowed "$old_kind" "$old_mode" \
                            "$old_root" "$old_relative"; then
                            old_manifest_error=1
                            break
                        fi
                        old_key=$old_root$tab$old_relative
                        if grep -Fqx -- "$old_key" "$old_seen_file"; then
                            old_manifest_error=1
                            break
                        fi
                        printf '%s\n' "$old_key" >> "$old_seen_file"
                        case "$old_root" in
                            P) old_base=$old_prefix ;;
                            D) old_base=$old_data ;;
                            *) old_manifest_error=1; break ;;
                        esac
                        old_target=$old_base/$old_relative
                        old_parent=$(realpath -m -- "$(dirname -- "$old_target")")
                        case "$old_parent" in
                            "$old_base"/*) ;;
                            *) old_manifest_error=1; break ;;
                        esac
                        printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
                            "$old_kind" "$old_hash" "$old_mode" "$old_root" \
                            "$old_relative" "$old_target" >> "$old_records_file"
                        old_record_count=$((old_record_count + 1))
                        ;;
                    *)
                        old_manifest_error=1
                        break
                        ;;
                esac
            done < "$manifest_target"
        fi
        if [ "$old_manifest_error" -eq 0 ] && [ "$old_record_count" -gt 0 ]; then
            old_manifest_format_valid=1
        fi
    fi

    if [ "$old_manifest_format_valid" -eq 1 ] && [ "$old_prefix" = "$prefix" ] && \
        [ "$old_data" != "$data_home" ]; then
        printf 'Refusing to change XDG_DATA_HOME for an existing Kukni installation.\n' >&2
        printf 'Run the installed uninstaller first, then install with the new data path.\n' >&2
        exit 1
    fi
    if [ "$old_manifest_format_valid" -eq 1 ] && [ "$old_prefix" = "$prefix" ] && \
        [ "$old_data" = "$data_home" ]; then
        old_manifest_valid=1
    elif [ "$force" -ne 1 ]; then
        printf 'Refusing to replace an unexpected Kukni ownership manifest: %s\n' \
            "$manifest_target" >&2
        printf 'Review it first, then rerun with --force if replacement is intended.\n' >&2
        exit 1
    fi
fi

root_path() {
    root_key=$1
    case "$root_key" in
        P) printf '%s\n' "$prefix" ;;
        D) printf '%s\n' "$data_home" ;;
        *)
            printf 'Unknown installation root: %s\n' "$root_key" >&2
            exit 1
            ;;
    esac
}

old_record_for() {
    lookup_root=$1
    lookup_relative=$2
    [ "$old_manifest_valid" -eq 1 ] || return 0
    awk -F '\t' -v root="$lookup_root" -v relative="$lookup_relative" \
        '$1 == "file" && $4 == root && $5 == relative { print $2 "\t" $3 }' \
        "$manifest_target"
}

check_target() {
    expected_file=$1
    expected_mode=$2
    target_root=$3
    target_relative=$4
    target_file=$5
    target_base=$6

    resolved_parent=$(realpath -m -- "$(dirname -- "$target_file")")
    case "$resolved_parent" in
        "$target_base"/*) ;;
        *)
            printf 'Refusing installation path that escapes its target root: %s\n' \
                "$target_file" >&2
            exit 1
            ;;
    esac

    if [ -d "$target_file" ] && [ ! -L "$target_file" ]; then
        printf 'Refusing to replace directory: %s\n' "$target_file" >&2
        exit 1
    fi
    if [ ! -e "$target_file" ] && [ ! -L "$target_file" ]; then
        return 0
    fi
    if [ -L "$target_file" ]; then
        if [ "$force" -eq 1 ]; then
            return 0
        fi
        printf 'Refusing to overwrite symbolic link: %s\n' "$target_file" >&2
        printf 'Review it first, then rerun with --force if replacement is intended.\n' >&2
        exit 1
    fi
    if [ ! -f "$target_file" ]; then
        printf 'Refusing to replace unexpected file type: %s\n' "$target_file" >&2
        exit 1
    fi

    current_mode=$(stat -c '%a' -- "$target_file")
    if [ "$current_mode" = "$expected_mode" ] && cmp -s -- "$expected_file" "$target_file"; then
        return 0
    fi

    old_record=$(old_record_for "$target_root" "$target_relative")
    case "$old_record" in
        *"$tab"*)
            old_hash=${old_record%%"$tab"*}
            old_mode=${old_record#*"$tab"}
            current_hash=$(sha256sum -- "$target_file" | awk '{ print $1 }')
            if [ "$current_hash" = "$old_hash" ] && [ "$current_mode" = "$old_mode" ]; then
                return 0
            fi
            ;;
    esac

    if [ "$force" -ne 1 ]; then
        printf 'Refusing to overwrite modified or unowned file: %s\n' "$target_file" >&2
        printf 'Review it first, then rerun with --force if replacement is intended.\n' >&2
        exit 1
    fi
}

check_launcher_link() {
    launcher_parent=$(dirname -- "$launcher_target")
    resolved_parent=$(realpath -m -- "$launcher_parent")
    case "$resolved_parent" in
        "$prefix"/*) ;;
        *)
            printf 'Refusing launcher path that escapes PREFIX: %s\n' \
                "$launcher_target" >&2
            exit 1
            ;;
    esac

    if [ -d "$launcher_target" ] && [ ! -L "$launcher_target" ]; then
        printf 'Refusing to replace directory: %s\n' "$launcher_target" >&2
        exit 1
    fi
    if [ ! -e "$launcher_target" ] && [ ! -L "$launcher_target" ]; then
        return 0
    fi
    if [ -L "$launcher_target" ]; then
        current_link_hash=$(readlink -n -- "$launcher_target" | \
            sha256sum | awk '{ print $1 }')
        expected_link_hash=$(printf '%s' "$launcher_link" | \
            sha256sum | awk '{ print $1 }')
        if [ "$current_link_hash" = "$expected_link_hash" ]; then
            return 0
        fi
    fi
    if [ "$force" -ne 1 ]; then
        printf 'Refusing to overwrite modified or unowned launcher: %s\n' \
            "$launcher_target" >&2
        printf 'Review it first, then rerun with --force if replacement is intended.\n' >&2
        exit 1
    fi
}

while IFS="$tab" read -r file_mode root_key relative_file; do
    base_path=$(root_path "$root_key")
    target_file=$base_path/$relative_file
    staged_file=$stage_root/$root_key/$relative_file
    check_target "$staged_file" "$file_mode" "$root_key" "$relative_file" \
        "$target_file" "$base_path"
done < "$plan_file"

check_launcher_link

obsolete_plan=$stage_root/obsolete
: > "$obsolete_plan"
if [ "$old_manifest_valid" -eq 1 ]; then
    while IFS="$tab" read -r obsolete_kind obsolete_hash obsolete_mode \
        obsolete_root obsolete_relative obsolete_target; do
        obsolete_key=$obsolete_root$tab$obsolete_relative
        if grep -Fqx -- "$obsolete_key" "$new_keys_file"; then
            continue
        fi
        if [ ! -e "$obsolete_target" ] && [ ! -L "$obsolete_target" ]; then
            continue
        fi
        if [ -d "$obsolete_target" ] && [ ! -L "$obsolete_target" ]; then
            printf 'Refusing to remove obsolete path that became a directory: %s\n' \
                "$obsolete_target" >&2
            exit 1
        fi

        obsolete_matches=0
        if [ "$obsolete_kind" = link ] && [ -L "$obsolete_target" ]; then
            current_obsolete_hash=$(readlink -n -- "$obsolete_target" | \
                sha256sum | awk '{ print $1 }')
            if [ "$current_obsolete_hash" = "$obsolete_hash" ]; then
                obsolete_matches=1
            fi
        elif [ "$obsolete_kind" = file ] && [ -f "$obsolete_target" ] && \
            [ ! -L "$obsolete_target" ]; then
            current_obsolete_hash=$(sha256sum -- "$obsolete_target" | awk '{ print $1 }')
            current_obsolete_mode=$(stat -c '%a' -- "$obsolete_target")
            if [ "$current_obsolete_hash" = "$obsolete_hash" ] && \
                [ "$current_obsolete_mode" = "$obsolete_mode" ]; then
                obsolete_matches=1
            fi
        fi

        if [ "$obsolete_matches" -ne 1 ] && [ "$force" -ne 1 ]; then
            printf 'Refusing to remove modified obsolete Kukni file: %s\n' \
                "$obsolete_target" >&2
            printf 'Review it first, then rerun with --force if removal is intended.\n' >&2
            exit 1
        fi
        printf '%s\n' "$obsolete_target" >> "$obsolete_plan"
    done < "$old_records_file"
fi

if [ "$old_manifest_valid" -ne 1 ]; then
    check_target "$manifest_stage" 644 P lib/kukni/.install-manifest \
        "$manifest_target" "$prefix"
fi

ensure_directory() {
    requested_directory=$1
    if [ -e "$requested_directory" ] || [ -L "$requested_directory" ]; then
        if [ ! -d "$requested_directory" ]; then
            printf 'Installation directory path is not a directory: %s\n' \
                "$requested_directory" >&2
            exit 1
        fi
        return 0
    fi

    missing_dirs=$(mktemp "$stage_root/missing-dirs.XXXXXX")
    directory_cursor=$requested_directory
    while [ ! -e "$directory_cursor" ] && [ ! -L "$directory_cursor" ]; do
        printf '%s\n' "$directory_cursor" >> "$missing_dirs"
        directory_parent=$(dirname -- "$directory_cursor")
        if [ "$directory_parent" = "$directory_cursor" ]; then
            printf 'Cannot find an existing parent for: %s\n' "$requested_directory" >&2
            exit 1
        fi
        directory_cursor=$directory_parent
    done
    if [ ! -d "$directory_cursor" ]; then
        printf 'Installation parent is not a directory: %s\n' "$directory_cursor" >&2
        exit 1
    fi

    awk '{ lines[NR] = $0 } END { for (line = NR; line > 0; line--) print lines[line] }' \
        "$missing_dirs" |
    while IFS= read -r directory_to_create; do
        mkdir -m 0755 -- "$directory_to_create"
        printf '%s\n' "$directory_to_create" >> "$created_dirs_file"
    done
    rm -f -- "$missing_dirs"
}

while IFS="$tab" read -r _file_mode root_key relative_file; do
    base_path=$(root_path "$root_key")
    ensure_directory "$(dirname -- "$base_path/$relative_file")"
done < "$plan_file"
ensure_directory "$app_root"
ensure_directory "$(dirname -- "$launcher_target")"

install_atomic() {
    atomic_source=$1
    atomic_mode=$2
    atomic_target=$3
    atomic_parent=$(dirname -- "$atomic_target")

    current_temp=$(mktemp "$atomic_parent/.kukni-install.XXXXXX")
    install -m "$atomic_mode" -- "$atomic_source" "$current_temp"

    atomic_backup=-
    if [ -e "$atomic_target" ] || [ -L "$atomic_target" ]; then
        atomic_backup=$(mktemp "$atomic_parent/.kukni-backup.XXXXXX")
        rm -f -- "$atomic_backup"
        cp -a -- "$atomic_target" "$atomic_backup"
    fi
    printf '%s\t%s\n' "$atomic_target" "$atomic_backup" >> "$applied_file"
    mv -f -- "$current_temp" "$atomic_target"
    current_temp=
}

install_link_atomic() {
    link_value=$1
    link_target=$2
    link_parent=$(dirname -- "$link_target")

    current_temp=$(mktemp "$link_parent/.kukni-install.XXXXXX")
    rm -f -- "$current_temp"
    ln -s -- "$link_value" "$current_temp"

    link_backup=-
    if [ -e "$link_target" ] || [ -L "$link_target" ]; then
        link_backup=$(mktemp "$link_parent/.kukni-backup.XXXXXX")
        rm -f -- "$link_backup"
        cp -a -- "$link_target" "$link_backup"
    fi
    printf '%s\t%s\n' "$link_target" "$link_backup" >> "$applied_file"
    mv -f -- "$current_temp" "$link_target"
    current_temp=
}

remove_atomic() {
    obsolete_target=$1
    if [ ! -e "$obsolete_target" ] && [ ! -L "$obsolete_target" ]; then
        return 0
    fi
    obsolete_parent=$(dirname -- "$obsolete_target")
    obsolete_backup=$(mktemp "$obsolete_parent/.kukni-backup.XXXXXX")
    rm -f -- "$obsolete_backup"
    printf '%s\t%s\n' "$obsolete_target" "$obsolete_backup" >> "$applied_file"
    mv -- "$obsolete_target" "$obsolete_backup"
}

while IFS="$tab" read -r file_mode root_key relative_file; do
    base_path=$(root_path "$root_key")
    install_atomic "$stage_root/$root_key/$relative_file" "$file_mode" \
        "$base_path/$relative_file"
done < "$plan_file"
install_link_atomic "$launcher_link" "$launcher_target"
while IFS= read -r obsolete_target; do
    [ -n "$obsolete_target" ] || continue
    remove_atomic "$obsolete_target"
done < "$obsolete_plan"
install_atomic "$manifest_stage" 644 "$manifest_target"

transaction_committed=1
while IFS="$tab" read -r _installed_target installed_backup; do
    if [ "$installed_backup" != - ]; then
        rm -f -- "$installed_backup"
    fi
done < "$applied_file"

# Session buses cache activation metadata. Reloading is best effort because the
# files are already committed and a headless install may have no session bus.
if [ "${KUKNI_SKIP_DBUS_RELOAD:-0}" != 1 ] && \
    command -v dbus-send >/dev/null 2>&1; then
    dbus-send --session --type=method_call \
        --dest=org.freedesktop.DBus /org/freedesktop/DBus \
        org.freedesktop.DBus.ReloadConfig >/dev/null 2>&1 || true
fi

printf 'Installed standalone Kukni for the current user.\n'
printf 'Run it directly with: %s FILE\n' "$launcher_target"
printf 'Kukni is registered as the Nautilus previewer for future D-Bus activations.\n'
printf 'If another previewer is already running, sign out and back in before testing Space.\n'
printf 'Uninstall with: %s\n' "$app_root/uninstall.sh"
