"""Build the app icons and logo images from the brand artwork (assets/brand/*.webp, white background).

    python tools/make_icons.py

Needs Pillow and NumPy (only for this one-time build step; the app itself does not use them).
Writes web/logo.png, web/icons/*.png and web/brand/*.png. The red sailboat is the app icon.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "assets" / "brand"
WEB = ROOT / "web"


def cutout(path: Path) -> Image.Image:
    """White background -> transparency, keeping anti-aliased edges (un-mixes the white)."""
    rgb = np.asarray(Image.open(path).convert("RGB")).astype(np.float32) / 255.0
    lo = rgb.min(axis=2)
    alpha = np.clip((1.0 - lo) / 0.9, 0.0, 1.0)          # pure colour has min channel <= ~0.1
    a = np.where(alpha > 1e-3, alpha, 1.0)[..., None]
    color = np.clip((rgb - (1.0 - alpha[..., None])) / a, 0.0, 1.0)
    out = np.dstack([color, alpha]) * 255.0
    img = Image.fromarray(out.astype(np.uint8), "RGBA")
    bbox = img.getchannel("A").point(lambda v: 255 if v > 10 else 0).getbbox()
    return img.crop(bbox)


def fit(img: Image.Image, box: int) -> Image.Image:
    w, h = img.size
    s = box / max(w, h)
    return img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)


def place(canvas: Image.Image, art: Image.Image, scale: float, dy: float = 0.02) -> Image.Image:
    n = canvas.size[0]
    a = fit(art, int(n * scale))
    x = (n - a.size[0]) // 2
    y = (n - a.size[1]) // 2 + int(n * dy)
    canvas.alpha_composite(a, (x, y))
    return canvas


def tile(n: int, radius: float | None, art: Image.Image, scale: float) -> Image.Image:
    """White tile with a very soft warm top light, the boat in the middle."""
    base = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    grad = Image.new("RGBA", (n, n))
    g = np.zeros((n, n, 4), np.float32)
    yy = np.linspace(0, 1, n)[:, None]
    top, bottom = np.array([255, 255, 255]), np.array([250, 244, 240])
    g[..., :3] = top * (1 - yy[..., None]) + bottom * yy[..., None]
    g[..., 3] = 255
    grad = Image.fromarray(g.astype(np.uint8), "RGBA")
    mask = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(mask)
    if radius is None:
        d.rectangle([0, 0, n, n], fill=255)
    else:
        d.rounded_rectangle([0, 0, n - 1, n - 1], radius=int(n * radius), fill=255)
    base.paste(grad, (0, 0), mask)
    shadow = fit(art, int(n * scale)).getchannel("A").filter(ImageFilter.GaussianBlur(n * 0.012))
    sh = Image.new("RGBA", shadow.size, (120, 20, 20, 0))
    sh.putalpha(shadow.point(lambda v: int(v * 0.18)))
    x = (n - shadow.size[0]) // 2
    y = (n - shadow.size[1]) // 2 + int(n * 0.035)
    base.alpha_composite(sh, (x, y))
    return place(base, art, scale)


def silhouette(art: Image.Image, n: int, scale: float, color=(255, 255, 255)) -> Image.Image:
    a = fit(art, int(n * scale))
    sil = Image.new("RGBA", a.size, color + (0,))
    sil.putalpha(a.getchannel("A"))
    c = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    c.alpha_composite(sil, ((n - a.size[0]) // 2, (n - a.size[1]) // 2))
    return c


def main() -> None:
    (WEB / "icons").mkdir(parents=True, exist_ok=True)
    (WEB / "brand").mkdir(parents=True, exist_ok=True)
    red = cutout(BRAND / "sailboat-original.webp")
    fit(red, 512).save(WEB / "logo.png", optimize=True)
    for n in (192, 512):
        tile(n, 0.225, red, 0.70).save(WEB / "icons" / f"icon-{n}.png", optimize=True)
        tile(n, None, red, 0.56).save(WEB / "icons" / f"maskable-{n}.png", optimize=True)
    tile(180, None, red, 0.66).save(WEB / "icons" / "apple-touch-icon.png", optimize=True)
    silhouette(red, 512, 0.62).save(WEB / "icons" / "monochrome-512.png", optimize=True)
    silhouette(red, 96, 0.80).save(WEB / "icons" / "badge-96.png", optimize=True)
    for name in ("blue", "gold", "navy"):
        f = BRAND / f"sailboat-{name}.webp"
        if f.exists():
            fit(cutout(f), 320).save(WEB / "brand" / f"sail-{name}.png", optimize=True)
    print("icons written to", WEB)


if __name__ == "__main__":
    main()
