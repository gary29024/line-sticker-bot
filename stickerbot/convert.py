"""Turning pictures into files Telegram accepts as stickers.

Telegram is strict about sticker files (from the official Bot API rules):

  Still sticker   : PNG or WEBP, one side exactly 512 px, the other 512 px or less,
                    at most 512 KB.
  Moving sticker  : WEBM video using the VP9 codec, one side exactly 512 px,
                    at most 3 seconds, at most 30 frames per second, no sound,
                    at most 256 KB.
  Custom emoji    : the same, but exactly 100 x 100 px.

LINE's animated stickers are "animated PNG" files, which Telegram doesn't
accept. So for those we:
  1. open the animation with Pillow (a picture library) and pull out every frame,
  2. resize each frame to 512 px,
  3. speed it up if it's longer than 3 seconds,
  4. hand the frames to ffmpeg (a video tool) to make a see-through VP9 video,
  5. if the video is bigger than 256 KB, try again at a lower quality.
"""

from __future__ import annotations

import asyncio
import bisect
import io
import math
import os
import shutil
import tempfile
from dataclasses import dataclass

from PIL import Image, ImageSequence

STICKER_SIDE = 512
EMOJI_SIDE = 100
MAX_STATIC_BYTES = 512 * 1024
MAX_VIDEO_BYTES = 256 * 1024
MAX_SECONDS = 2.9  # Telegram's limit is 3.0 s; stay a little under to be safe
MAX_FPS = 30
MIN_FPS = 10
MAX_INPUT_PIXELS = 40_000_000  # refuse gigantic images
LONG_VIDEO_SECONDS = 6.0  # videos longer than this are cut instead of sped up

# Quality ladder for video stickers: (crf, highest frame rate). A higher crf
# means a smaller file but a blurrier picture, so we start sharp and only get
# blurrier if the file is still too big.
VIDEO_ATTEMPTS = [(30, 30), (35, 30), (40, 30), (45, 24), (50, 20), (55, 15), (63, 12)]

FFMPEG = os.environ.get("FFMPEG_PATH", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_PATH", "ffprobe")


class ConvertError(Exception):
    """A file couldn't be turned into a sticker. The message is safe to show users."""


@dataclass
class Converted:
    data: bytes
    format: str  # "static" or "video" - the words Telegram uses
    extension: str  # "png", "webp" or "webm"

    @property
    def filename(self) -> str:
        return f"sticker.{self.extension}"


# --------------------------------------------------------------------------
# Sizing
# --------------------------------------------------------------------------


def _even(n: float) -> int:
    """Round to an even number (video encoders prefer even sizes)."""
    return max(2, int(round(n / 2)) * 2)


def target_size(width: int, height: int, emoji: bool) -> tuple[int, int]:
    """How big the picture itself should be (inside the final canvas)."""
    side = EMOJI_SIDE if emoji else STICKER_SIDE
    scale = side / max(width, height)
    if width >= height:
        return side, min(side, _even(height * scale))
    return min(side, _even(width * scale)), side


def _fit_frame(frame: Image.Image, emoji: bool) -> Image.Image:
    """Resize one frame. Emoji are centred on a see-through 100x100 square."""
    frame = frame.convert("RGBA")
    w, h = target_size(frame.width, frame.height, emoji)
    if (w, h) != frame.size:
        frame = frame.resize((w, h), Image.LANCZOS)
    if emoji and (w, h) != (EMOJI_SIDE, EMOJI_SIDE):
        canvas = Image.new("RGBA", (EMOJI_SIDE, EMOJI_SIDE), (0, 0, 0, 0))
        canvas.paste(frame, ((EMOJI_SIDE - w) // 2, (EMOJI_SIDE - h) // 2))
        frame = canvas
    return frame


# --------------------------------------------------------------------------
# Still stickers
# --------------------------------------------------------------------------


def to_static(data: bytes, emoji: bool = False) -> Converted:
    """Any still picture (PNG, JPG, WEBP, the first frame of a GIF...) -> Telegram still sticker."""
    image = _open_image(data)
    if image is None:
        raise ConvertError("I couldn't read that picture.")
    image.seek(0)
    frame = _fit_frame(image, emoji)

    buf = io.BytesIO()
    frame.save(buf, "PNG", optimize=True)
    if buf.tell() <= MAX_STATIC_BYTES:
        return Converted(buf.getvalue(), "static", "png")
    # Too big as PNG: WEBP is smaller. Lower quality step by step until it fits.
    for quality in (95, 90, 80, 70, 60, 50, 40):
        buf = io.BytesIO()
        frame.save(buf, "WEBP", quality=quality, method=6)
        if buf.tell() <= MAX_STATIC_BYTES:
            return Converted(buf.getvalue(), "static", "webp")
    raise ConvertError("That picture is too detailed to fit Telegram's 512 KB limit.")


def _open_image(data: bytes) -> Image.Image | None:
    try:
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > MAX_INPUT_PIXELS:
            raise ConvertError("That picture is far too large.")
        return image
    except ConvertError:
        raise
    except Exception:
        return None


# --------------------------------------------------------------------------
# Moving stickers
# --------------------------------------------------------------------------


def _image_frames(data: bytes, emoji: bool) -> tuple[list[Image.Image], list[float]]:
    """The frames of an animated PNG / GIF / WEBP, already resized, plus how long each shows (ms).

    To save memory, frames that would never be seen in the final 30-frames-per-second
    sticker are skipped (their time is added to the frame before them).
    """
    # Pass 1: just read how long each frame lasts.
    image = Image.open(io.BytesIO(data))
    is_gif = image.format == "GIF"
    durations: list[float] = []
    total, too_long = 0.0, False
    for frame in ImageSequence.Iterator(image):
        duration = float(frame.info.get("duration") or 0)
        if duration <= 0 or (is_gif and duration < 20):
            duration = 100.0  # what web browsers do with "no delay" frames
        durations.append(duration)
        total += duration
        if total > LONG_VIDEO_SECONDS * 1000 or len(durations) >= 2000:
            too_long = True
            break
    if too_long:
        # A long clip: keep only the first ~3 seconds rather than fast-forwarding it.
        kept, t = [], 0.0
        for d in durations:
            if t >= MAX_SECONDS * 1000:
                break
            kept.append(min(d, MAX_SECONDS * 1000 - t))
            t += d
        durations = kept
        total = sum(durations)

    # Pass 2: keep at most one frame per 1/30 s of final sticker time.
    speed = max(1.0, total / (MAX_SECONDS * 1000))
    slot = 1000 / MAX_FPS * speed
    frames: list[Image.Image] = []
    kept_durations: list[float] = []
    next_slot, t = 0.0, 0.0
    image = Image.open(io.BytesIO(data))
    for index, frame in enumerate(ImageSequence.Iterator(image)):
        if index >= len(durations):
            break
        if not frames or t >= next_slot - 1e-6:
            frames.append(_fit_frame(frame.copy(), emoji))
            kept_durations.append(durations[index])
            next_slot = (math.floor(t / slot + 1e-6) + 1) * slot
        else:
            kept_durations[-1] += durations[index]
        t += durations[index]
    return frames, kept_durations


async def _run(*args: str, stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin)
    return proc.returncode, out, err


async def _video_frames(data: bytes, emoji: bool) -> tuple[list[Image.Image], list[float]]:
    """Frames from a real video file (MP4, WEBM, MOV, GIF-as-MP4), using ffmpeg."""
    with tempfile.TemporaryDirectory(prefix="stk-") as tmp:
        src = os.path.join(tmp, "input")
        with open(src, "wb") as f:
            f.write(data)
        # What kind of video is it, and how long? See-through WEBM videos need the
        # libvpx decoder, otherwise the transparency is lost.
        code, out, _ = await _run(
            FFPROBE, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name:format=duration", "-of", "default=nw=1", src,
        )
        info = dict(line.split("=", 1) for line in out.decode(errors="replace").splitlines() if "=" in line)
        if code != 0 or not info.get("codec_name"):
            raise ConvertError("I couldn't read that file as a picture or a video.")
        decoder = {"vp9": ["-c:v", "libvpx-vp9"], "vp8": ["-c:v", "libvpx"]}.get(info["codec_name"], [])
        try:
            seconds = float(info.get("duration", "0"))
        except ValueError:
            seconds = 0.0

        if seconds > LONG_VIDEO_SECONDS:
            # A long clip: keep only the first ~3 seconds rather than fast-forwarding it.
            limit, speed = MAX_SECONDS, 1.0
        else:
            # A short clip: grab just enough frames to speed it up into 3 seconds.
            limit, speed = LONG_VIDEO_SECONDS, max(1.0, seconds / MAX_SECONDS)
        code, _, err = await _run(
            FFMPEG, "-hide_banner", "-loglevel", "error", *decoder, "-i", src,
            "-t", str(limit), "-an", "-vf", f"fps={MAX_FPS / speed:.4f}",
            "-pix_fmt", "rgba", os.path.join(tmp, "f%04d.png"),
        )
        names = sorted(n for n in os.listdir(tmp) if n.startswith("f") and n.endswith(".png"))
        if code != 0 or not names:
            raise ConvertError("I couldn't read the frames of that video.")
        frames = []
        for name in names[: int(LONG_VIDEO_SECONDS * MAX_FPS)]:
            with Image.open(os.path.join(tmp, name)) as im:
                frames.append(_fit_frame(im, emoji))
    durations = [1000 / MAX_FPS * speed] * len(frames)
    return frames, durations


def plan_timeline(durations: list[float], fps_cap: int) -> tuple[int, list[int]]:
    """Decide the output frame rate and which source frame to show at each output frame.

    If the animation is longer than MAX_SECONDS it gets sped up to fit.
    Returns (frames_per_second, [source frame index for each output frame]).
    """
    total_ms = sum(durations)
    speed = max(1.0, total_ms / (MAX_SECONDS * 1000))
    # Use just enough frames per second to show the shortest frame, capped at fps_cap.
    shortest = min(durations) / speed
    fps = int(min(fps_cap, max(MIN_FPS, math.ceil(1000 / shortest - 1e-6))))
    out_seconds = min(MAX_SECONDS, total_ms / 1000 / speed)
    count = max(1, int(math.floor(out_seconds * fps + 1e-6)))

    starts, t = [], 0.0
    for d in durations:
        starts.append(t)
        t += d
    order = []
    for i in range(count):
        source_ms = (i / fps) * speed * 1000
        order.append(max(0, bisect.bisect_right(starts, source_ms) - 1))
    return fps, order


async def _encode_webm(frames: list[Image.Image], order: list[int], fps: int, crf: int) -> bytes:
    width, height = frames[0].size
    raw = [frame.tobytes() for frame in frames]
    with tempfile.TemporaryDirectory(prefix="stk-") as tmp:
        out_path = os.path.join(tmp, "out.webm")
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{width}x{height}",
            "-framerate", str(fps), "-i", "pipe:0",
            "-an", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
            "-crf", str(crf), "-b:v", "0",
            "-deadline", "good", "-cpu-used", "4", "-row-mt", "1",
            "-f", "webm", out_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        stderr_task = asyncio.create_task(proc.stderr.read())
        try:
            for index in order:
                proc.stdin.write(raw[index])
                await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
        err = await stderr_task
        await proc.wait()
        if proc.returncode != 0 or not os.path.exists(out_path):
            raise ConvertError(f"The video tool failed: {err.decode(errors='replace')[-200:]}")
        with open(out_path, "rb") as f:
            return f.read()


async def to_video(data: bytes, emoji: bool = False) -> Converted:
    """Any animation or video -> Telegram moving sticker (VP9 WEBM)."""
    image = _open_image(data)
    if image is not None and getattr(image, "n_frames", 1) > 1:
        frames, durations = await asyncio.to_thread(_image_frames, data, emoji)
    elif image is not None:
        return await asyncio.to_thread(to_static, data, emoji)
    else:
        frames, durations = await _video_frames(data, emoji)

    if len(frames) <= 1:
        return await asyncio.to_thread(to_static, data, emoji)

    smallest = None
    for crf, fps_cap in VIDEO_ATTEMPTS:
        fps, order = plan_timeline(durations, fps_cap)
        webm = await _encode_webm(frames, order, fps, crf)
        if len(webm) <= MAX_VIDEO_BYTES:
            return Converted(webm, "video", "webm")
        smallest = len(webm) if smallest is None else min(smallest, len(webm))
    raise ConvertError(
        f"That animation is too detailed to fit Telegram's 256 KB limit (best try was {smallest // 1024} KB)."
    )


def looks_animated(data: bytes) -> bool:
    """Quick check: does this file move? (Animated PNG/GIF/WEBP, or something that isn't a picture at all.)"""
    image = _open_image(data)
    if image is None:
        return True  # not a picture Pillow knows - probably a video
    return getattr(image, "n_frames", 1) > 1


async def convert(data: bytes, emoji: bool = False) -> Converted:
    """The one function the rest of the bot uses: give it a file, get a sticker back."""
    if data[:4] == b"\x1f\x8b\x08\x00" or data[:2] == b"\x1f\x8b":
        raise ConvertError("Animated .tgs stickers can't be converted (they're vector drawings, not pictures).")
    if looks_animated(data):
        return await to_video(data, emoji)
    return await asyncio.to_thread(to_static, data, emoji)


def ffmpeg_available() -> bool:
    return shutil.which(FFMPEG) is not None and shutil.which(FFPROBE) is not None
