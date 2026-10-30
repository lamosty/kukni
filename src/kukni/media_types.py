# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Media metadata selection shared by passive previews and experimental playback."""

import math
from gi.repository import Gio, GLib


MEDIA_SUFFIXES = (
    ".3gp",
    ".3g2",
    ".aac",
    ".aif",
    ".aiff",
    ".avi",
    ".asf",
    ".flv",
    ".flac",
    ".m4a",
    ".m4v",
    ".mka",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ts",
    ".mts",
    ".m2ts",
    ".oga",
    ".ogg",
    ".ogv",
    ".opus",
    ".wav",
    ".webm",
    ".wma",
    ".wmv",
)

APPLICATION_MEDIA_TYPES = frozenset(
    {
        "application/ogg",
        "application/x-matroska",
        "application/x-ogg",
    }
)

PLAYLIST_CONTENT_TYPES = frozenset(
    {
        "application/m3u",
        "application/mpegurl",
        "application/ram",
        "application/smil+xml",
        "application/vnd.apple.mpegurl",
        "application/vnd.ms-asf",
        "application/vnd.ms-wpl",
        "application/x-mpegurl",
        "application/x-ms-asx",
        "application/x-quicktime-media-link",
        "application/x-quicktimeplayer",
        "application/x-xspf+xml",
        "application/xspf+xml",
        "audio/m3u",
        "audio/mpegurl",
        "audio/scpls",
        "audio/x-m3u",
        "audio/x-mp3-playlist",
        "audio/x-ms-asx",
        "audio/x-mpegurl",
        "audio/x-scpls",
        "video/vnd.mpegurl",
        "video/x-mpegurl",
        "video/x-ms-asx",
        "video/x-ms-wax",
        "video/x-ms-wmx",
        "video/x-ms-wvx",
    }
)

GENERIC_CONTENT_TYPES = frozenset(
    {
        "",
        "application/octet-stream",
        "application/x-empty",
        "inode/x-empty",
    }
)


def supports_media(basename: str | None, content_type: str | None) -> bool:
    """Return whether trusted file metadata identifies supported local media.

    MIME information wins.  A suffix is considered only when the MIME type is
    absent or generic, so a file named ``movie.mkv`` but identified as an image
    is not handed to a media decoder.  Playlists are excluded because they can
    reference remote resources.
    """

    normalized_type = (content_type or "").split(";", 1)[0].strip().casefold()
    if normalized_type in PLAYLIST_CONTENT_TYPES:
        return False
    if normalized_type.startswith(("audio/", "video/")):
        return True
    if normalized_type in APPLICATION_MEDIA_TYPES:
        return True

    generic_type = normalized_type in GENERIC_CONTENT_TYPES
    if normalized_type and not generic_type:
        try:
            generic_type = Gio.content_type_is_unknown(normalized_type)
        except (TypeError, GLib.Error):
            generic_type = False
    normalized_name = (basename or "").casefold()
    return generic_type and normalized_name.endswith(MEDIA_SUFFIXES)


def format_media_time(seconds: float | int | None) -> str:
    """Format a non-negative duration without allowing unbounded output."""

    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value) or value < 0:
        value = 0.0
    whole_seconds = min(int(value), 359_999_999)
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds_part:02d}"
    return f"{minutes}:{seconds_part:02d}"
