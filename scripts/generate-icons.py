#!/usr/bin/env python3
"""Resize the supplied artwork for the app and hicolor icon theme."""
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data/io.github.DevVoidPL.NarratorVerse.png'


def generate():
    with Image.open(SOURCE) as source:
        image = source.convert('RGBA')
    size = max(image.size)
    canvas = Image.new('RGBA', (size, size))
    canvas.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
    folder = ROOT / 'data/icons'
    canvas.save(folder / 'artwork.png')
    mask = Image.new('L', canvas.size)
    inset = round(size * 0.025)
    ImageDraw.Draw(mask).ellipse((inset, inset, size - inset - 1, size - inset - 1), fill=255)
    canvas.putalpha(ImageChops.multiply(canvas.getchannel('A'), mask))
    taskbar = canvas.crop(mask.getbbox())

    def render(size):
        artwork = taskbar if size in (24, 32) else canvas
        return artwork.resize((size, size), Image.Resampling.LANCZOS)

    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        render(size).save(folder / f'{size}x{size}.png')
    for name, size in (('32x32.png', 32), ('128x128.png', 128), ('128x128@2x.png', 256), ('icon.png', 512)):
        render(size).save(ROOT / 'src-tauri/icons' / name)


if __name__ == '__main__':
    generate()
