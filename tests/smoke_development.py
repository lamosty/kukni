#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Prove development previews are local and isolated from desktop services."""

import os
from pathlib import Path
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib

from image_fixtures import png
from kukni.application import (
    APPLICATION_ID,
    DEVELOPMENT_APPLICATION_ID,
    DEVELOPMENT_APPLICATION_NAME,
    KukniApplication,
)
from kukni.renderers.image_view import ImagePreviewView
from kukni.renderers.image import ImageRenderer
from kukni.session import PreviewState
from kukni.nautilus_previewer import BUS_NAME as PREVIEWER_BUS_NAME


DBUS = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
DBUS_INTERFACE = "org.freedesktop.DBus"


def bus_call(connection, method, parameters, reply_signature):
    return connection.call_sync(
        DBUS,
        DBUS_PATH,
        DBUS_INTERFACE,
        method,
        parameters,
        GLib.VariantType.new(reply_signature),
        Gio.DBusCallFlags.NONE,
        2_000,
        None,
    ).unpack()


def request_name(connection, name):
    # 4 is DBUS_NAME_FLAG_DO_NOT_QUEUE; 1 is DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER.
    reply, = bus_call(connection, "RequestName", GLib.Variant("(su)", (name, 4)), "(u)")
    if reply != 1:
        raise AssertionError(f"sentinel could not own {name}: reply {reply}")


def name_has_owner(connection, name):
    owned, = bus_call(connection, "NameHasOwner", GLib.Variant("(s)", (name,)), "(b)")
    return owned


def name_owner(connection, name):
    owner, = bus_call(connection, "GetNameOwner", GLib.Variant("(s)", (name,)), "(s)")
    return owner


def sentinel_connection():
    return Gio.DBusConnection.new_for_address_sync(
        os.environ["DBUS_SESSION_BUS_ADDRESS"],
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None,
        None,
    )


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory(prefix="kukni-development-smoke-") as temporary:
        temporary = Path(temporary)
        home = temporary / "home"
        data_home = home / ".local/share"
        home.mkdir()
        os.environ["HOME"] = str(home)
        os.environ["XDG_DATA_HOME"] = str(data_home)

        sample = temporary / "literal development pixels.png"
        sample.write_bytes(png(37, 23))

        sentinel = sentinel_connection()
        request_name(sentinel, APPLICATION_ID)
        production_owner = sentinel.get_unique_name()
        application = KukniApplication(development=True)
        polls = 0
        saw_opening = False
        verified = False

        # Force at least one main-loop observation of OPENING, then call the
        # unchanged worker path. This catches a smoke test that quits on its
        # first poll without ever accepting real decoded pixels.
        original_prepare = ImageRenderer._prepare

        def delayed_prepare(renderer, path, *, cancelled):
            time.sleep(0.15)
            return original_prepare(renderer, path, cancelled=cancelled)

        ImageRenderer._prepare = delayed_prepare

        def verify():
            nonlocal polls, saw_opening, verified
            polls += 1
            window = application.get_active_window()
            if window is None or window.session.snapshot.state is PreviewState.OPENING:
                if window is not None:
                    saw_opening = True
                if polls < 200:
                    return GLib.SOURCE_CONTINUE
                failures.append("development PNG preview timed out")
                application.quit()
                return GLib.SOURCE_REMOVE
            try:
                snapshot = window.session.snapshot
                if snapshot.state is not PreviewState.PREVIEW:
                    raise AssertionError(
                        f"synthetic PNG used {snapshot.state.value}: {snapshot.detail}"
                    )
                view = window._stack.get_child_by_name("content")
                if not isinstance(view, ImagePreviewView):
                    raise AssertionError("synthetic PNG did not use the real image preview")
                if (view.texture.get_width(), view.texture.get_height()) != (37, 23):
                    raise AssertionError("synthetic PNG pixels have the wrong dimensions")

                if application.get_application_id() != DEVELOPMENT_APPLICATION_ID:
                    raise AssertionError("development application ID is not isolated")
                if not application.get_flags() & Gio.ApplicationFlags.NON_UNIQUE:
                    raise AssertionError("development application can forward to a stale process")
                if application.get_is_remote():
                    raise AssertionError("development preview was forwarded instead of run locally")
                if window.get_title() != DEVELOPMENT_APPLICATION_NAME:
                    raise AssertionError("development window is not visibly labelled")
                if GLib.get_application_name() != DEVELOPMENT_APPLICATION_NAME:
                    raise AssertionError("development application label is missing")
                badge = window._development_badge
                if badge.get_label() != "Development":
                    raise AssertionError("development header marker has the wrong label")
                if not badge.get_visible() or not badge.get_mapped():
                    raise AssertionError("development header marker is not visibly mapped")
                if not saw_opening:
                    raise AssertionError("test did not observe the deliberately delayed worker")

                if name_owner(sentinel, APPLICATION_ID) != production_owner:
                    raise AssertionError("development launch displaced the production owner")
                if name_has_owner(sentinel, DEVELOPMENT_APPLICATION_ID):
                    raise AssertionError("non-unique development launch acquired a bus name")
                if name_has_owner(sentinel, PREVIEWER_BUS_NAME):
                    raise AssertionError("development launch acquired Nautilus preview integration")
                verified = True
            except Exception as error:  # pragma: no cover - smoke diagnostics
                failures.append(str(error))
            finally:
                application.quit()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(50, verify)
        # The launcher inserts `--` so even dash-prefixed file names remain
        # files rather than becoming GApplication control flags.
        try:
            exit_code = application.run(["kukni-development-smoke", "--", str(sample)])
        finally:
            ImageRenderer._prepare = original_prepare
        sentinel.close_sync(None)

        if not verified:
            failures.append("development application exited before PNG verification")

        forbidden = (
            data_home / "applications/io.github.lamosty.Kukni.desktop",
            data_home / "dbus-1/services/io.github.lamosty.Kukni.service",
            data_home / "dbus-1/services/org.gnome.NautilusPreviewer.service",
        )
        if any(path.exists() or path.is_symlink() for path in forbidden):
            failures.append("development launch created desktop or D-Bus registration")

    if exit_code != 0:
        failures.append(f"development application exited with status {exit_code}")
    if failures:
        for failure in failures:
            print(f"development smoke failure: {failure}", file=sys.stderr)
        return 1
    print("Development isolation and real PNG smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
