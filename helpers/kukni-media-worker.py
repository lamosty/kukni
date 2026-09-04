#!/usr/bin/python3
# Copyright (C) 2026 Kukni contributors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Decode one local media preview inside Kukni's disposable sandbox.

The parent process supplies the sandbox and resource limits.  This helper still
validates every part of its small protocol so it fails closed if it is invoked
incorrectly.  It intentionally has no GTK, display, sound-server, or network
integration: audio is consumed by a ``fakesink`` and video crosses the process
boundary as one tightly packed RGBA frame.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import math
import os
import stat
import sys
from typing import Iterator, NoReturn, Sequence


PROTOCOL_VERSION = 1
FRAME_FORMAT_RGBA8 = "rgba8"
FRAME_FORMAT_NONE = "none"

INPUT_PATH = "/input/media"
FRAME_OUTPUT_PATH = "/output/frame.rgba"
RESULT_OUTPUT_PATH = "/output/result.json"

# These are protocol ceilings, not policy knobs.  A parent may request lower
# limits, but never expand the worker beyond the contract understood by Kukni.
HARD_MAX_INPUT_BYTES = 16 * 1024 * 1024 * 1024
HARD_MAX_EDGE_PIXELS = 1_800
HARD_MAX_FRAME_BYTES = 1_800 * 1_800 * 4
HARD_MAX_RESULT_BYTES = 64 * 1024
HARD_MAX_DURATION_USEC = 359_999_999 * 1_000_000
STATE_CHANGE_TIMEOUT_SECONDS = 10

FAILURE_MESSAGE = "kukni-media-worker: preview failed\n"
FAILURE_BYTES = FAILURE_MESSAGE.encode("ascii")

_CLI_FLAGS = (
    "--input",
    "--frame-output",
    "--result-output",
    "--max-edge",
    "--max-frame-bytes",
    "--max-result-bytes",
    "--max-input-bytes",
)


class WorkerError(RuntimeError):
    """A request or decoded frame violated the worker contract."""


@dataclass(frozen=True, slots=True)
class WorkerLimits:
    max_edge_pixels: int
    max_frame_bytes: int
    max_result_bytes: int
    max_input_bytes: int


@dataclass(frozen=True, slots=True)
class DecodedMedia:
    """Trusted-in-shape, but not yet protocol-validated, decoder output."""

    kind: str
    has_video: bool
    has_audio: bool
    duration_usec: int
    width: int
    height: int
    frame: bytes


@dataclass(frozen=True, slots=True)
class WorkerFiles:
    input_fd: int
    frame_fd: int
    result_fd: int


def _fail() -> NoReturn:
    raise WorkerError("invalid media worker request")


def _parse_bounded_decimal(value: str, maximum: int) -> int:
    """Parse the parent's canonical positive base-ten integer spelling."""

    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
    ):
        _fail()
    parsed = int(value, 10)
    if parsed <= 0 or parsed > maximum:
        _fail()
    return parsed


def parse_cli(argv: Sequence[str]) -> WorkerLimits:
    """Accept only the exact internal command emitted by the parent contract."""

    if isinstance(argv, (str, bytes)) or len(argv) != len(_CLI_FLAGS) * 2:
        _fail()
    arguments = tuple(argv)
    if any(not isinstance(argument, str) for argument in arguments):
        _fail()
    if arguments[::2] != _CLI_FLAGS:
        _fail()
    if arguments[1] != INPUT_PATH:
        _fail()
    if arguments[3] != FRAME_OUTPUT_PATH:
        _fail()
    if arguments[5] != RESULT_OUTPUT_PATH:
        _fail()

    limits = WorkerLimits(
        max_edge_pixels=_parse_bounded_decimal(
            arguments[7], HARD_MAX_EDGE_PIXELS
        ),
        max_frame_bytes=_parse_bounded_decimal(
            arguments[9], HARD_MAX_FRAME_BYTES
        ),
        max_result_bytes=_parse_bounded_decimal(
            arguments[11], HARD_MAX_RESULT_BYTES
        ),
        max_input_bytes=_parse_bounded_decimal(
            arguments[13], HARD_MAX_INPUT_BYTES
        ),
    )
    if limits.max_frame_bytes < limits.max_edge_pixels * 4:
        _fail()
    return limits


def _open_path(path: str, flags: int) -> int:
    allowed_paths = {INPUT_PATH, FRAME_OUTPUT_PATH, RESULT_OUTPUT_PATH}
    if path not in allowed_paths:
        _fail()
    safe_flags = flags | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    # O_NONBLOCK ensures a substituted FIFO cannot hang before fstat rejects it.
    safe_flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        return os.open(path, safe_flags)
    except OSError as error:
        raise WorkerError("media worker file could not be opened") from error


@contextmanager
def open_worker_files(limits: WorkerLimits) -> Iterator[WorkerFiles]:
    """Open the three fixed sandbox files and truncate only validated outputs."""

    descriptors: list[int] = []
    try:
        input_fd = _open_path(INPUT_PATH, os.O_RDONLY)
        descriptors.append(input_fd)
        frame_fd = _open_path(FRAME_OUTPUT_PATH, os.O_WRONLY)
        descriptors.append(frame_fd)
        result_fd = _open_path(RESULT_OUTPUT_PATH, os.O_WRONLY)
        descriptors.append(result_fd)

        try:
            input_stat = os.fstat(input_fd)
            frame_stat = os.fstat(frame_fd)
            result_stat = os.fstat(result_fd)
        except OSError as error:
            raise WorkerError("media worker file could not be inspected") from error
        if not all(
            stat.S_ISREG(item.st_mode)
            for item in (input_stat, frame_stat, result_stat)
        ):
            raise WorkerError("media worker files must be regular files")
        if input_stat.st_size <= 0 or input_stat.st_size > limits.max_input_bytes:
            raise WorkerError("media worker input size is invalid")
        identities = {
            (item.st_dev, item.st_ino)
            for item in (input_stat, frame_stat, result_stat)
        }
        if len(identities) != 3:
            raise WorkerError("media worker files must be distinct")

        # @constraint Do not pass O_TRUNC to open: all inode identities are
        # checked before either capability can modify data.
        try:
            for descriptor in (frame_fd, result_fd):
                os.ftruncate(descriptor, 0)
                os.lseek(descriptor, 0, os.SEEK_SET)
        except OSError as error:
            raise WorkerError("media worker output could not be prepared") from error

        yield WorkerFiles(input_fd, frame_fd, result_fd)
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def pack_rgba_rows(
    data: bytes | bytearray | memoryview,
    *,
    width: int,
    height: int,
    stride: int,
    offset: int,
    max_frame_bytes: int,
) -> bytes:
    """Copy a possibly padded/negative-stride RGBA plane into tight rows."""

    integer_values = (width, height, stride, offset, max_frame_bytes)
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in integer_values
    ):
        raise WorkerError("invalid RGBA frame layout")
    if width <= 0 or height <= 0 or max_frame_bytes <= 0 or offset < 0:
        raise WorkerError("invalid RGBA frame layout")

    row_bytes = width * 4
    expected_bytes = row_bytes * height
    if expected_bytes > max_frame_bytes or abs(stride) < row_bytes:
        raise WorkerError("invalid RGBA frame layout")
    try:
        source = memoryview(data).cast("B")
    except (TypeError, ValueError) as error:
        raise WorkerError("invalid RGBA frame data") from error

    output = bytearray(expected_bytes)
    for row in range(height):
        source_start = offset + row * stride
        source_end = source_start + row_bytes
        if source_start < 0 or source_end > source.nbytes:
            raise WorkerError("RGBA frame data is truncated")
        output_start = row * row_bytes
        output[output_start : output_start + row_bytes] = source[
            source_start:source_end
        ]
    return bytes(output)


def _load_gstreamer():
    """Import multimedia bindings lazily so protocol tests need no GI runtime."""

    try:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import Gst, GstVideo

        initialized, _arguments = Gst.init_check(None)
    except Exception as error:
        raise WorkerError("GStreamer is unavailable") from error
    if not initialized:
        raise WorkerError("GStreamer could not be initialized")
    return Gst, GstVideo


def _require_element(Gst, factory_name: str):
    element = Gst.ElementFactory.make(factory_name, None)
    if element is None:
        raise WorkerError("a required GStreamer element is unavailable")
    return element


def _build_video_sink(Gst, limits: WorkerLimits):
    """Build the only pixel path: raw RGBA into one bounded appsink queue."""

    video_bin = Gst.Bin.new(None)
    if video_bin is None:
        raise WorkerError("the GStreamer video bin could not be created")
    convert = _require_element(Gst, "videoconvert")
    scale = _require_element(Gst, "videoscale")
    caps_filter = _require_element(Gst, "capsfilter")
    app_sink = _require_element(Gst, "appsink")
    for element in (convert, scale, caps_filter, app_sink):
        video_bin.add(element)

    # Bounding both axes at negotiation time prevents an oversized raw frame
    # from entering the one-buffer queue.  Product size is checked again after
    # preroll because caps cannot express width*height.
    safe_edge = min(
        limits.max_edge_pixels,
        math.isqrt(limits.max_frame_bytes // 4),
    )
    if safe_edge <= 0:
        raise WorkerError("the requested frame limit is too small")
    caps = Gst.Caps.from_string(
        "video/x-raw,format=RGBA,"
        f"width=(int)[1,{safe_edge}],height=(int)[1,{safe_edge}],"
        "pixel-aspect-ratio=(fraction)1/1"
    )
    if caps is None or caps.is_empty():
        raise WorkerError("the RGBA caps could not be created")
    caps_filter.set_property("caps", caps)
    app_sink.set_property("max-buffers", 1)
    app_sink.set_property("drop", True)
    app_sink.set_property("enable-last-sample", False)
    app_sink.set_property("emit-signals", False)
    app_sink.set_property("sync", False)

    if not (
        convert.link(scale)
        and scale.link(caps_filter)
        and caps_filter.link(app_sink)
    ):
        raise WorkerError("the GStreamer video path could not be linked")
    sink_pad = convert.get_static_pad("sink")
    if sink_pad is None:
        raise WorkerError("the GStreamer video path has no input pad")
    ghost_pad = Gst.GhostPad.new("sink", sink_pad)
    if ghost_pad is None or not video_bin.add_pad(ghost_pad):
        raise WorkerError("the GStreamer video path could not be exposed")
    return video_bin, app_sink


def _duration_usec(Gst, pipeline) -> int:
    try:
        available, duration = pipeline.query_duration(Gst.Format.TIME)
    except Exception as error:
        raise WorkerError("media duration could not be queried") from error
    if not available or duration in (-1, Gst.CLOCK_TIME_NONE):
        return 0
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
        raise WorkerError("media duration is invalid")
    duration_usec = duration // 1_000
    if duration_usec > HARD_MAX_DURATION_USEC:
        raise WorkerError("media duration exceeds the protocol limit")
    return duration_usec


def _sample_to_frame(Gst, GstVideo, sample, limits: WorkerLimits) -> tuple[int, int, bytes]:
    """Validate negotiated RGBA metadata and copy exactly one mapped plane."""

    if sample is None:
        raise WorkerError("the video stream returned no preroll")
    caps = sample.get_caps()
    buffer = sample.get_buffer()
    if (
        caps is None
        or buffer is None
        or not caps.is_fixed()
        or caps.get_size() != 1
    ):
        raise WorkerError("the video preroll is incomplete")
    structure = caps.get_structure(0)
    if (
        structure is None
        or structure.get_name() != "video/x-raw"
        or structure.get_string("format") != "RGBA"
    ):
        raise WorkerError("the video preroll is not RGBA")

    try:
        info = GstVideo.VideoInfo.new_from_caps(caps)
        width = int(info.width)
        height = int(info.height)
        stride = int(info.stride[0])
        offset = int(info.offset[0])
    except Exception as error:
        raise WorkerError("the video layout could not be read") from error
    if (
        width <= 0
        or height <= 0
        or width > limits.max_edge_pixels
        or height > limits.max_edge_pixels
        or width * height * 4 > limits.max_frame_bytes
    ):
        raise WorkerError("the video dimensions exceed the worker limits")

    try:
        video_meta = GstVideo.buffer_get_video_meta(buffer)
    except Exception as error:
        raise WorkerError("the video metadata could not be read") from error
    if video_meta is not None:
        try:
            meta_width = int(video_meta.width)
            meta_height = int(video_meta.height)
            meta_planes = int(video_meta.n_planes)
            meta_stride = int(video_meta.stride[0])
            meta_offset = int(video_meta.offset[0])
        except Exception as error:
            raise WorkerError("the video metadata is invalid") from error
        if (
            video_meta.format != GstVideo.VideoFormat.RGBA
            or meta_planes != 1
            or (meta_width, meta_height) != (width, height)
        ):
            raise WorkerError("the video metadata dimensions disagree")
        stride, offset = meta_stride, meta_offset

    try:
        buffer_size = int(buffer.get_size())
    except Exception as error:
        raise WorkerError("the video buffer size could not be read") from error
    if buffer_size <= 0 or buffer_size > limits.max_frame_bytes:
        raise WorkerError("the video buffer exceeds the worker limit")

    mapped = None
    try:
        mapped_ok, mapped = buffer.map(Gst.MapFlags.READ)
        if not mapped_ok or mapped is None:
            raise WorkerError("the video buffer could not be mapped")
        if mapped.size <= 0 or mapped.size > limits.max_frame_bytes:
            raise WorkerError("the mapped video buffer exceeds the worker limit")
        frame = pack_rgba_rows(
            mapped.data,
            width=width,
            height=height,
            stride=stride,
            offset=offset,
            max_frame_bytes=limits.max_frame_bytes,
        )
    except WorkerError:
        raise
    except Exception as error:
        raise WorkerError("the video buffer could not be copied") from error
    finally:
        if mapped is not None:
            try:
                buffer.unmap(mapped)
            except Exception:
                pass
    if len(frame) != width * height * 4:
        raise WorkerError("the copied video frame has the wrong size")
    return width, height, frame


def decode_media(input_fd: int, limits: WorkerLimits) -> DecodedMedia:
    """Pause one fixed local playbin and return at most one RGBA preroll."""

    if isinstance(input_fd, bool) or not isinstance(input_fd, int) or input_fd < 0:
        raise WorkerError("the media input descriptor is invalid")
    Gst, GstVideo = _load_gstreamer()
    pipeline = None
    try:
        pipeline = _require_element(Gst, "playbin")
        audio_sink = _require_element(Gst, "fakesink")
        video_sink, app_sink = _build_video_sink(Gst, limits)
        audio_sink.set_property("sync", False)
        audio_sink.set_property("async", False)

        # GstPlayFlags is private to playbin.  Values 1 and 2 enable only video
        # and audio: subtitles, visualizers, buffering/download, and software
        # volume are deliberately absent.
        pipeline.set_property("flags", 1 | 2)
        pipeline.set_property("audio-sink", audio_sink)
        pipeline.set_property("video-sink", video_sink)
        # Decode the descriptor that was opened and fstat-validated above.  A
        # pathname is never reopened, even if this helper is run without its
        # normal immutable bind mount.
        pipeline.set_property("uri", f"fd://{input_fd}")

        transition = pipeline.set_state(Gst.State.PAUSED)
        if transition == Gst.StateChangeReturn.FAILURE:
            raise WorkerError("the media pipeline could not be paused")
        result = pipeline.get_state(STATE_CHANGE_TIMEOUT_SECONDS * Gst.SECOND)
        if not result or result[0] != Gst.StateChangeReturn.SUCCESS:
            raise WorkerError("the media pipeline did not preroll")

        try:
            video_count = pipeline.get_property("n-video")
            audio_count = pipeline.get_property("n-audio")
        except Exception as error:
            raise WorkerError("media stream metadata could not be read") from error
        if (
            isinstance(video_count, bool)
            or not isinstance(video_count, int)
            or video_count < 0
            or isinstance(audio_count, bool)
            or not isinstance(audio_count, int)
            or audio_count < 0
        ):
            raise WorkerError("media stream metadata is invalid")
        has_video = video_count > 0
        has_audio = audio_count > 0
        if not has_video and not has_audio:
            raise WorkerError("the media file has no audio or video stream")

        duration_usec = _duration_usec(Gst, pipeline)
        if not has_video:
            return DecodedMedia(
                kind="audio",
                has_video=False,
                has_audio=True,
                duration_usec=duration_usec,
                width=0,
                height=0,
                frame=b"",
            )

        # @constraint Pull exactly one PAUSED-state preroll.  Never switch to
        # PLAYING and never request a second sample from attacker-controlled
        # decoder code.
        sample = app_sink.emit("pull-preroll")
        width, height, frame = _sample_to_frame(
            Gst, GstVideo, sample, limits
        )
        return DecodedMedia(
            kind="video",
            has_video=True,
            has_audio=has_audio,
            duration_usec=duration_usec,
            width=width,
            height=height,
            frame=frame,
        )
    except WorkerError:
        raise
    except Exception as error:
        raise WorkerError("media decoding failed") from error
    finally:
        if pipeline is not None:
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass


def encode_result(media: DecodedMedia, limits: WorkerLimits) -> tuple[bytes, bytes]:
    """Validate decoder output and encode one exact protocol-v1 object."""

    if not isinstance(media, DecodedMedia):
        raise WorkerError("invalid decoded media result")
    if not isinstance(media.has_video, bool) or not isinstance(media.has_audio, bool):
        raise WorkerError("invalid decoded stream flags")
    if (
        isinstance(media.duration_usec, bool)
        or not isinstance(media.duration_usec, int)
        or media.duration_usec < 0
        or media.duration_usec > HARD_MAX_DURATION_USEC
    ):
        raise WorkerError("invalid decoded duration")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (media.width, media.height)
    ):
        raise WorkerError("invalid decoded dimensions")
    if not isinstance(media.frame, bytes):
        raise WorkerError("invalid decoded frame")

    if media.kind == "audio":
        if media.has_video or not media.has_audio:
            raise WorkerError("inconsistent audio stream flags")
        if media.width or media.height or media.frame:
            raise WorkerError("audio media unexpectedly contains a frame")
        frame_format = FRAME_FORMAT_NONE
    elif media.kind == "video":
        if not media.has_video or media.width <= 0 or media.height <= 0:
            raise WorkerError("inconsistent video stream metadata")
        if (
            media.width > limits.max_edge_pixels
            or media.height > limits.max_edge_pixels
        ):
            raise WorkerError("decoded dimensions exceed the worker limit")
        expected_bytes = media.width * media.height * 4
        if (
            expected_bytes > limits.max_frame_bytes
            or len(media.frame) != expected_bytes
        ):
            raise WorkerError("decoded frame size is invalid")
        frame_format = FRAME_FORMAT_RGBA8
    else:
        raise WorkerError("invalid decoded media kind")

    value = {
        "version": PROTOCOL_VERSION,
        "kind": media.kind,
        "has_video": media.has_video,
        "has_audio": media.has_audio,
        "duration_usec": media.duration_usec,
        "width": media.width,
        "height": media.height,
        "frame_format": frame_format,
        "frame_bytes": len(media.frame),
    }
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError) as error:
        raise WorkerError("media result could not be encoded") from error
    if not payload or len(payload) > limits.max_result_bytes:
        raise WorkerError("media result exceeds the worker limit")
    return media.frame, payload


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        try:
            count = os.write(descriptor, view[written:])
        except OSError as error:
            raise WorkerError("media worker output could not be written") from error
        if count <= 0:
            raise WorkerError("media worker output write made no progress")
        written += count


def run(limits: WorkerLimits) -> None:
    """Perform one transaction, publishing metadata only after frame success."""

    with open_worker_files(limits) as files:
        media = decode_media(files.input_fd, limits)
        frame, result = encode_result(media, limits)
        _write_all(files.frame_fd, frame)
        _write_all(files.result_fd, result)


def _save_and_silence_stderr() -> int:
    """Keep one reporting FD while sending native-library logging to /dev/null."""

    report_fd = -1
    try:
        report_fd = os.dup(2)
        os.set_inheritable(report_fd, False)
    except OSError as error:
        if report_fd >= 0:
            try:
                os.close(report_fd)
            except OSError:
                pass
        raise WorkerError("media worker error reporting is unavailable") from error

    null_fd = -1
    try:
        null_fd = os.open(
            "/dev/null",
            os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
        )
        os.dup2(null_fd, 2, inheritable=False)
    except OSError as error:
        _report_failure(report_fd)
        try:
            os.close(report_fd)
        except OSError:
            pass
        raise WorkerError("media worker logging could not be silenced") from error
    finally:
        if null_fd >= 0:
            try:
                os.close(null_fd)
            except OSError:
                pass
    return report_fd


def _report_failure(report_fd: int) -> None:
    """Best-effort one fixed write; never format a caught exception."""

    try:
        os.write(report_fd, FAILURE_BYTES)
    except BaseException:
        pass


def main(argv: Sequence[str] | None = None) -> int:
    """Run with native stderr silenced and one fixed diagnostic capability."""

    report_fd = -1
    try:
        try:
            # @constraint This happens before the lazy GI/GStreamer import.
            # Native codec warnings therefore cannot reach the saved parent
            # stream through conventional stderr logging.
            report_fd = _save_and_silence_stderr()
        except BaseException:
            return 1
        try:
            limits = parse_cli(sys.argv[1:] if argv is None else argv)
            run(limits)
        except BaseException:
            _report_failure(report_fd)
            return 1
        return 0
    finally:
        if report_fd >= 0:
            try:
                os.close(report_fd)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
