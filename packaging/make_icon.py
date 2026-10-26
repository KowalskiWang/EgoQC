"""Draws the EgoQC icon (two lenses + check mark) into an .iconset folder for iconutil."""
import os, sys

from PIL import Image, ImageDraw


def draw(size):
    s = size / 1024
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([int(80 * s), int(80 * s), int(944 * s), int(944 * s)], radius=int(190 * s), fill=(28, 92, 170, 255))
    d.rounded_rectangle([int(360 * s), int(405 * s), int(664 * s), int(435 * s)], radius=int(12 * s), fill=(236, 243, 252, 255))
    for cx in (360, 664):
        r = 150
        d.ellipse([int((cx - r) * s), int((420 - r) * s), int((cx + r) * s), int((420 + r) * s)], fill=(236, 243, 252, 255))
        r = 78
        d.ellipse([int((cx - r) * s), int((420 - r) * s), int((cx + r) * s), int((420 + r) * s)], fill=(20, 40, 70, 255))
    pts = [(330, 720), (460, 830), (700, 610)]
    d.line([(int(x * s), int(y * s)) for x, y in pts], fill=(120, 220, 140, 255), width=max(2, int(70 * s)), joint="curve")
    return im


out = sys.argv[1]
os.makedirs(out, exist_ok=True)
for base in (16, 32, 128, 256, 512):
    draw(base).save(os.path.join(out, f"icon_{base}x{base}.png"))
    draw(base * 2).save(os.path.join(out, f"icon_{base}x{base}@2x.png"))
