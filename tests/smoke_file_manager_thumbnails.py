#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Exercise the installed thumbnail registration through GNOME's own sandbox."""

from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gi
gi.require_version("GnomeDesktop", "4.0")
from gi.repository import GnomeDesktop
from kukni.media_diagnostics import generate_sample


def main():
    with tempfile.TemporaryDirectory(prefix="kukni-desktop-thumbnail-") as temporary:
        path = Path(temporary) / "blue.wmv"
        generate_sample(path, "wmv")
        factory = GnomeDesktop.DesktopThumbnailFactory.new(GnomeDesktop.DesktopThumbnailSize.LARGE)
        pixbuf = factory.generate_thumbnail(path.as_uri(), "video/x-ms-wmv", None)
        if pixbuf is None or pixbuf.get_width() != 64 or pixbuf.get_height() != 48:
            raise AssertionError("GNOME did not generate the expected WMV thumbnail")
        data = pixbuf.get_pixels()
        if not (data[0] < 20 and data[1] < 20 and data[2] > 230):
            raise AssertionError("GNOME's thumbnail pixels do not match the WMV")
    print("GNOME Files WMV thumbnail: Passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
