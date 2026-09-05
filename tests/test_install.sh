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

fail() {
    printf 'install test failed: %s\n' "$1" >&2
    exit 1
}

expect_failure() {
    if "$@" >/dev/null 2>&1; then
        fail "command unexpectedly succeeded: $*"
    fi
}

unset SUDO_USER SUDO_UID || true
export KUKNI_SKIP_DBUS_RELOAD=1
export HOME="$test_root/home"
mkdir -p -- "$HOME"

# Paths whose escaping differs between Desktop Entry and D-Bus syntax are
# rejected before any installation destination is created.
for hostile_suffix in 'back\slash' 'double"quote' 'back`tick' 'dollar$sign'; do
    hostile_prefix=$test_root/$hostile_suffix
    expect_failure env HOME="$HOME" PREFIX="$hostile_prefix" \
        XDG_DATA_HOME="$test_root/hostile-data" KUKNI_SKIP_DBUS_RELOAD=1 \
        "$project_dir/install.sh"
    test ! -e "$hostile_prefix" || fail 'hostile PREFIX created files'
done

export PREFIX="$test_root/prefix with space%"
export XDG_DATA_HOME="$test_root/data with space%"
app_root=$PREFIX/lib/kukni
launcher=$PREFIX/bin/kukni
desktop=$XDG_DATA_HOME/applications/io.github.lamosty.Kukni.desktop
app_service=$XDG_DATA_HOME/dbus-1/services/io.github.lamosty.Kukni.service
previewer_service=$XDG_DATA_HOME/dbus-1/services/org.gnome.NautilusPreviewer.service
manifest=$app_root/.install-manifest

# Existing private directory modes must remain private. An existing activation
# override is also a conflict until the user explicitly supplies --force.
mkdir -p -- "$PREFIX/bin" "$app_root" \
    "$XDG_DATA_HOME/applications" "$XDG_DATA_HOME/dbus-1/services"
chmod 0700 -- "$PREFIX/bin" "$app_root" \
    "$XDG_DATA_HOME/applications" "$XDG_DATA_HOME/dbus-1/services"
printf '%s\n' 'foreign previewer configuration' > "$previewer_service"
expect_failure "$project_dir/install.sh"
test ! -e "$launcher" && test ! -L "$launcher" || \
    fail 'conflict left a partial launcher installation'
test "$(cat "$previewer_service")" = 'foreign previewer configuration' || \
    fail 'conflict changed the foreign previewer service'

"$project_dir/install.sh" --force >/dev/null

# The private runtime retains bin/src/helpers layout. The public command points
# to a tiny -B launcher so normal execution cannot create bytecode in that tree.
test -L "$launcher" || fail '~/.local/bin/kukni is not a symbolic link'
test "$(readlink -- "$launcher")" = '../lib/kukni/launcher/kukni' || \
    fail 'launcher symbolic link does not target the private launcher'
cmp -- "$project_dir/packaging/kukni-launcher" "$app_root/launcher/kukni"
cmp -- "$project_dir/bin/kukni" "$app_root/bin/kukni"
cmp -- "$project_dir/helpers/kukni-cr2-worker.py" \
    "$app_root/helpers/kukni-cr2-worker.py"
cmp -- "$project_dir/helpers/kukni-image-worker.py" \
    "$app_root/helpers/kukni-image-worker.py"
cmp -- "$project_dir/helpers/kukni-extract-preview.py" \
    "$app_root/helpers/kukni-extract-preview.py"
cmp -- "$project_dir/helpers/kukni-media-worker.py" \
    "$app_root/helpers/kukni-media-worker.py"
find "$project_dir/src/kukni" -type f \
    \( -name '*.py' -o -name '*.css' \) ! -path '*/__pycache__/*' -print |
while IFS= read -r source_file; do
    relative_file=${source_file#"$project_dir/"}
    cmp -- "$source_file" "$app_root/$relative_file"
done

test -f "$manifest" || fail 'ownership manifest was not installed'
test ! -e "$XDG_DATA_HOME/sushi" || fail 'standalone installer wrote legacy previewer files'

outer_link_mode=$(stat -c '%a' "$launcher")
private_launcher_mode=$(stat -c '%a' "$app_root/launcher/kukni")
inner_launcher_mode=$(stat -c '%a' "$app_root/bin/kukni")
source_mode=$(stat -c '%a' "$app_root/src/kukni/application.py")
service_mode=$(stat -c '%a' "$previewer_service")
test "$outer_link_mode" = 777 || fail 'public launcher is not a normal symbolic link'
test "$private_launcher_mode" = 755 || fail 'private launcher mode is not 755'
test "$inner_launcher_mode" = 755 || fail 'application launcher mode is not 755'
test "$source_mode" = 644 || fail 'Python source mode is not 644'
test "$service_mode" = 644 || fail 'D-Bus service mode is not 644'
for private_dir in "$PREFIX/bin" "$app_root" \
    "$XDG_DATA_HOME/applications" "$XDG_DATA_HOME/dbus-1/services"; do
    test "$(stat -c '%a' "$private_dir")" = 700 || \
        fail "installer changed existing directory mode: $private_dir"
done

# The mandatory runtime probe and private launcher are both exercised without
# opening a display. -B plus PYTHONDONTWRITEBYTECODE must leave no local cache.
"$launcher" --help >/dev/null
if find "$app_root" -type d -name __pycache__ -print | grep -q .; then
    fail 'normal launcher execution created bytecode in the installed tree'
fi

desktop_exec=$(printf '%s' "$launcher" | sed 's/%/%%/g')
grep -Fqx "Exec=\"$desktop_exec\" %U" "$desktop" || \
    fail 'desktop Exec path was not safely rendered'
grep -Fqx 'DBusActivatable=true' "$desktop" || \
    fail 'desktop entry does not request D-Bus activation'
grep -Fqx 'Name=io.github.lamosty.Kukni' "$app_service" || \
    fail 'application D-Bus service name is wrong'
grep -Fqx "Exec=\"$launcher\" --gapplication-service" "$app_service" || \
    fail 'application D-Bus Exec path is wrong'
grep -Fqx 'Name=org.gnome.NautilusPreviewer' "$previewer_service" || \
    fail 'previewer override name is wrong'
grep -Fqx "Exec=\"$launcher\" --gapplication-service" "$previewer_service" || \
    fail 'previewer Exec path is wrong'

if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$desktop"
else
    printf 'desktop-file-validate unavailable; metadata parser check skipped\n'
fi

# Exercise each generated service file on its own fresh private session bus,
# without launching GTK or contacting the user's real bus. The fake activator
# owns only the name requested for that run, proving D-Bus selected that file.
if command -v dbus-run-session >/dev/null 2>&1 && \
    command -v gdbus >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
    fake_activator=$test_root/fake-activator.py
    cat > "$fake_activator" <<'PY'
#!/usr/bin/python3
from gi.repository import Gio, GLib

connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
name = __import__("os").environ["KUKNI_FAKE_NAME"]
reply = connection.call_sync(
    "org.freedesktop.DBus",
    "/org/freedesktop/DBus",
    "org.freedesktop.DBus",
    "RequestName",
    GLib.Variant("(su)", (name, 4)),
    GLib.VariantType.new("(u)"),
    Gio.DBusCallFlags.NONE,
    -1,
    None,
)
if reply.unpack()[0] not in (1, 4):
    raise SystemExit(1)
loop = GLib.MainLoop()
connection.connect("closed", lambda *_args: loop.quit())
GLib.timeout_add_seconds(10, loop.quit)
loop.run()
PY
    chmod 0755 -- "$fake_activator"
    activation_client=$test_root/activation-client.sh
    cat > "$activation_client" <<'SH'
#!/bin/sh
set -eu
name=${1:?service name is required}
result=$(gdbus call --session \
    --dest org.freedesktop.DBus \
    --object-path /org/freedesktop/DBus \
    --method org.freedesktop.DBus.StartServiceByName \
    "$name" 0)
case "$result" in
    *'uint32 1'*|*'uint32 2'*) ;;
    *) exit 1 ;;
esac
owner=$(gdbus call --session \
    --dest org.freedesktop.DBus \
    --object-path /org/freedesktop/DBus \
    --method org.freedesktop.DBus.NameHasOwner "$name")
case "$owner" in
    *true*) ;;
    *) exit 1 ;;
esac
SH
    chmod 0755 -- "$activation_client"
    original_launcher_link=$(readlink -- "$launcher")
    rm -- "$launcher"
    ln -s -- "$fake_activator" "$launcher"
    for activation_name in \
        io.github.lamosty.Kukni org.gnome.NautilusPreviewer; do
        if ! timeout 15 env HOME="$HOME" XDG_DATA_HOME="$XDG_DATA_HOME" \
            KUKNI_FAKE_NAME="$activation_name" \
            dbus-run-session -- "$activation_client" "$activation_name" \
            >/dev/null 2>&1; then
            fail "isolated D-Bus activation failed: $activation_name"
        fi
    done
    rm -- "$launcher"
    ln -s -- "$original_launcher_link" "$launcher"
else
    printf 'isolated D-Bus tools unavailable; activation check skipped\n'
fi

# Reinstalling identical owned files is idempotent.
"$project_dir/install.sh" >/dev/null

# A late conflict must be found before an earlier absent file is restored.
rm -- "$app_root/bin/kukni"
printf '\nlocal change\n' >> "$previewer_service"
expect_failure "$project_dir/install.sh"
test ! -e "$app_root/bin/kukni" || \
    fail 'failed preflight partially restored an earlier target'
"$project_dir/install.sh" --force >/dev/null

# Fail a real mv during commit. Rollback must restore edited content, leave a
# previously absent target absent, and put the original manifest back.
printf '\nrollback sentinel\n' >> "$app_root/NOTICE.md"
rm -- "$app_root/VERSION"
manifest_before=$(sha256sum "$manifest" | awk '{ print $1 }')
fake_bin=$test_root/fake-bin
mkdir -p -- "$fake_bin"
real_mv=$(command -v mv)
cat > "$fake_bin/mv" <<'SH'
#!/bin/sh
set -eu
count=0
if [ -f "$KUKNI_MV_COUNT_FILE" ]; then
    IFS= read -r count < "$KUKNI_MV_COUNT_FILE"
fi
count=$((count + 1))
printf '%s\n' "$count" > "$KUKNI_MV_COUNT_FILE"
if [ "$count" -eq "$KUKNI_FAIL_MV_AT" ]; then
    exit 73
fi
exec "$KUKNI_REAL_MV" "$@"
SH
chmod 0755 -- "$fake_bin/mv"
expect_failure env PATH="$fake_bin:$PATH" KUKNI_REAL_MV="$real_mv" \
    KUKNI_MV_COUNT_FILE="$test_root/mv-count" KUKNI_FAIL_MV_AT=4 \
    "$project_dir/install.sh" --force
grep -Fq 'rollback sentinel' "$app_root/NOTICE.md" || \
    fail 'rollback did not restore modified content'
test ! -e "$app_root/VERSION" || fail 'rollback retained a newly restored target'
test "$(sha256sum "$manifest" | awk '{ print $1 }')" = "$manifest_before" || \
    fail 'rollback changed the ownership manifest'
if find "$PREFIX" "$XDG_DATA_HOME" -name '.kukni-install.*' -o \
    -name '.kukni-backup.*' | grep -q .; then
    fail 'rollback left transaction files behind'
fi
"$project_dir/install.sh" --force >/dev/null

# A valid old-manifest record absent from the new release is removed during a
# successful upgrade, then forgotten only after the transaction commits.
obsolete_file=$app_root/obsolete-runtime-file
printf '%s\n' 'obsolete owned content' > "$obsolete_file"
chmod 0644 -- "$obsolete_file"
obsolete_hash=$(sha256sum "$obsolete_file" | awk '{ print $1 }')
printf 'file\t%s\t644\tP\tlib/kukni/obsolete-runtime-file\n' \
    "$obsolete_hash" >> "$manifest"
"$project_dir/install.sh" >/dev/null
test ! -e "$obsolete_file" || fail 'upgrade retained an obsolete owned file'
if grep -Fq 'obsolete-runtime-file' "$manifest"; then
    fail 'new manifest retained an obsolete record'
fi

# A changed data root is refused even with --force so the old user activation
# override cannot become an unowned, still-active file.
new_data_home=$test_root/new-data-home
expect_failure env HOME="$HOME" PREFIX="$PREFIX" XDG_DATA_HOME="$new_data_home" \
    KUKNI_SKIP_DBUS_RELOAD=1 "$project_dir/install.sh" --force
test -e "$previewer_service" || fail 'data-root refusal removed old activation'
test ! -e "$new_data_home" || fail 'data-root refusal created new activation data'

# Uninstall preflights every owned file. Without --force, one edit leaves all
# matching files in place. With --force, unknown files are still preserved.
printf '\nlocal change\n' >> "$app_root/src/kukni/application.py"
printf '%s\n' 'keep me' > "$app_root/local-note"
expect_failure "$project_dir/uninstall.sh"
test -e "$desktop" || fail 'failed uninstall partially removed desktop metadata'
"$project_dir/uninstall.sh" --force >/dev/null
test ! -e "$launcher" && test ! -L "$launcher" || \
    fail 'launcher survived forced uninstall'
test ! -e "$desktop" || fail 'desktop entry survived forced uninstall'
test ! -e "$app_service" || fail 'application service survived forced uninstall'
test ! -e "$previewer_service" || fail 'previewer service survived forced uninstall'
test -f "$app_root/local-note" || fail 'uninstaller removed an unowned file'
rm -- "$app_root/local-note"
rmdir -- "$app_root"

# The installed uninstaller remembers custom data paths without extra variables.
"$project_dir/install.sh" >/dev/null
installed_uninstaller=$app_root/uninstall.sh
unset PREFIX XDG_DATA_HOME
"$installed_uninstaller" >/dev/null
test ! -e "$test_root/prefix with space%/bin/kukni" && \
    test ! -L "$test_root/prefix with space%/bin/kukni" || \
    fail 'installed uninstaller did not derive its custom prefix'
test ! -e "$test_root/data with space%/dbus-1/services/org.gnome.NautilusPreviewer.service" || \
    fail 'installed uninstaller did not use manifest data home'

# Defaults form a conventional ~/.local installation and include no legacy tree.
export HOME="$test_root/default-home"
mkdir -p -- "$HOME"
unset PREFIX XDG_DATA_HOME
"$project_dir/install.sh" >/dev/null
test -x "$HOME/.local/bin/kukni" || fail 'default launcher is not in ~/.local/bin'
test -f "$HOME/.local/lib/kukni/src/kukni/application.py" || \
    fail 'default private application tree is missing'
test -f "$HOME/.local/share/applications/io.github.lamosty.Kukni.desktop" || \
    fail 'default desktop entry is missing'
test -f "$HOME/.local/share/dbus-1/services/io.github.lamosty.Kukni.service" || \
    fail 'default application service is missing'
test -f "$HOME/.local/share/dbus-1/services/org.gnome.NautilusPreviewer.service" || \
    fail 'default previewer override is missing'
test ! -e "$HOME/.local/share/sushi" || fail 'default install created a legacy data tree'
"$project_dir/uninstall.sh" >/dev/null

# Sudo markers are rejected even when the effective UID itself is non-root.
export PREFIX="$test_root/sudo-prefix"
export XDG_DATA_HOME="$test_root/sudo-data"
SUDO_UID=1000; export SUDO_UID
expect_failure "$project_dir/install.sh"
test ! -e "$PREFIX/bin/kukni" && test ! -L "$PREFIX/bin/kukni" || \
    fail 'sudo-marked install wrote a launcher'
unset SUDO_UID

sh -n "$project_dir/install.sh" "$project_dir/uninstall.sh" \
    "$project_dir/packaging/kukni-launcher"
PYTHONPYCACHEPREFIX="$test_root/python-cache" \
    python3 -m py_compile "$project_dir/packaging/render-template.py"
if grep -i 'sushi' "$project_dir/install.sh" "$project_dir/uninstall.sh" >/dev/null; then
    fail 'standalone installer output still names the removed previewer'
fi

printf 'standalone install/uninstall tests passed\n'
