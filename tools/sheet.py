"""Put several phone screenshots side by side (for design reviews and before/after pages).

    python tools/sheet.py OUT.png IMG1.png IMG2.png ... [--scale 0.5] [--label "text"]...
"""
from __future__ import annotations

import sys

from PIL import Image, ImageDraw


def main(argv: list[str]) -> None:
    scale = 0.5
    labels: list[str] = []
    files: list[str] = []
    it = iter(argv[1:])
    out = argv[0]
    for a in it:
        if a == "--scale":
            scale = float(next(it))
        elif a == "--label":
            labels.append(next(it))
        else:
            files.append(a)
    ims = [Image.open(f).convert("RGB") for f in files]
    ims = [i.resize((int(i.width * scale), int(i.height * scale))) for i in ims]
    pad, top = 16, (34 if labels else 0)
    w = sum(i.width for i in ims) + pad * (len(ims) + 1)
    h = max(i.height for i in ims) + pad * 2 + top
    o = Image.new("RGB", (w, h), (32, 33, 38))
    d = ImageDraw.Draw(o)
    x = pad
    for n, i in enumerate(ims):
        if n < len(labels):
            d.text((x, pad), labels[n], fill=(230, 230, 235))
        o.paste(i, (x, pad + top))
        x += i.width + pad
    o.save(out)


if __name__ == "__main__":
    main(sys.argv[1:])
