# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Synthetic public-domain pixels, not photographs or downloaded test corpora."""

import base64
import struct
import zlib


def png(width=120, height=80, *, transparent=False):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend((int(40 + 180 * x / width), int(70 + 100 * y / height), 190, 128 if transparent else 255))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


# A synthetic 12×8 solid-blue WebP and GIF, encoded once to keep Pillow out of
# the runtime and test dependencies. The normal PNG fixture is built above.
WEBP = base64.b64decode('UklGRjgAAABXRUJQVlA4ICwAAADwAQCdASoMAAgAAUAmJaACdLoB+AAETAAA/u+9V/43bjDfgu/33oDeBgAAAA==')
GIF = base64.b64decode('R0lGODdhDAAIAIEAADNmmQAAAAAAAAAAACwAAAAADAAIAAAIEgABCBxIsKDBgwgTKlzIsGHBgAA7')
