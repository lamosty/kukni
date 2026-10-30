# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later

"""Fixed synthetic media checks through the installed worker boundary."""

from pathlib import Path
import tempfile
import wave


def generate_sample(path: Path, kind: str) -> None:
    if kind == "wav":
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\0\0" * 8000)
        return
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    # libav's encoder offsets timestamps; normalize its segment before the
    # ASF muxer, which otherwise writes a synthetic multi-hour duration.
    encoders = {"wmv": "avenc_wmv2 ! identity single-segment=true ! avmux_asf",
                "mkv": "vp8enc threads=1 deadline=1 ! matroskamux"}
    pipeline = Gst.parse_launch(
        "videotestsrc num-buffers=6 pattern=blue ! "
        "video/x-raw,width=64,height=48,framerate=6/1 ! videoconvert ! "
        + encoders[kind] + " ! filesink name=output"
    )
    pipeline.get_by_name("output").set_property("location", str(path))
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("synthetic encoder unavailable")
        message = pipeline.get_bus().timed_pop_filtered(
            10 * Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS,
        )
        if message is None or message.type != Gst.MessageType.EOS:
            raise RuntimeError("synthetic encoder failed")
    finally:
        pipeline.set_state(Gst.State.NULL)


def verify_output(output, kind):
    result = output.result
    if not 500_000 <= result.duration_usec <= 2_000_000:
        raise ValueError("incorrect duration")
    if kind == "wav":
        if result.has_video or not result.has_audio or output.frame:
            raise ValueError("incorrect audio metadata")
    else:
        if not result.has_frame or result.width != 64 or result.height != 48:
            raise ValueError("incorrect frame dimensions")
        # The fixture is solid blue; validate actual pixels, not just a READY
        # state or a successful subprocess that may have returned no picture.
        pixels = output.frame
        if not all(r < 20 and g < 20 and b > 230 and a == 255
                   for r, g, b, a in zip(pixels[::4], pixels[1::4], pixels[2::4], pixels[3::4])):
            raise ValueError("incorrect frame pixels")


def main():
    from .media_worker import MediaWorkerError, run_media_worker
    stage = "startup"
    try:
        with tempfile.TemporaryDirectory(prefix="kukni-media-check-") as temporary:
            for kind in ("wmv", "mkv", "wav"):
                path = Path(temporary) / ("sample." + kind)
                stage = f"generate {kind.upper()}"
                generate_sample(path, kind)
                stage = f"decode {kind.upper()}"
                output = run_media_worker(path)
                stage = f"pixels and duration {kind.upper()}"
                verify_output(output, kind)
                print(f"Media {kind.upper()} self-test: Passed")
        return 0
    except MediaWorkerError as error:
        # These are fixed supervisor diagnostics; decoder-controlled stderr
        # and exception strings never cross its process boundary.
        exit_detail = f", exit {error.exit_code}" if type(error.exit_code) is int else ""
        print(f"Media self-test: Failed ({stage}: {error}{exit_detail})")
        return 1
    except Exception:
        print(f"Media self-test: Failed ({stage})")
        return 1
