import io
import os
import sys

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_png(size=(370, 320), color=(255, 120, 0, 255)) -> bytes:
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([10, 10, size[0] - 10, size[1] - 10], fill=color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_apng(size=(370, 320), frames=10, duration=100, loop=3) -> bytes:
    images = []
    for i in range(frames):
        img = Image.new("RGBA", size, (0, 0, 0, 0))
        x = 10 + i * (size[0] - 80) // max(1, frames - 1)
        ImageDraw.Draw(img).ellipse([x, 40, x + 60, 100], fill=(30, 144, 255, 255))
        images.append(img)
    buf = io.BytesIO()
    images[0].save(buf, "PNG", save_all=True, append_images=images[1:], duration=duration, loop=loop)
    return buf.getvalue()


def make_gif(size=(200, 150), frames=8) -> bytes:
    images = [Image.new("RGB", size, (i * 30, 100, 200 - i * 20)) for i in range(frames)]
    buf = io.BytesIO()
    images[0].save(buf, "GIF", save_all=True, append_images=images[1:], duration=80, loop=0)
    return buf.getvalue()


@pytest.fixture
def png_bytes():
    return make_png()


@pytest.fixture
def apng_bytes():
    return make_apng()
