"""The morning picture of the city: every building drawn from its day art as a skyline."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

IMG = Path(__file__).parent / "static" / "img"
W, H = 1280, 720
GROUND = 600  # where the towers stand
PLINTH = 128  # each tower's base plinth, in picture pixels
SKY = [(122, 167, 245), (185, 180, 248), (245, 198, 227)]


def _sky(Image):
    col = Image.new("RGB", (1, H))
    for y in range(H):
        t = y / (H - 1)
        a, b, u = (SKY[0], SKY[1], t / .5) if t < .5 else (SKY[1], SKY[2], (t - .5) / .5)
        col.putpixel((0, y), tuple(round(a[i] + (b[i] - a[i]) * u) for i in range(3)))
    return col.resize((W, H))


def _tower(Image, ImageEnhance, key: str, spr: dict, floors: int, most: int, dark: bool):
    """Base, one floor piece per few floors, roof: the same stacking the Mini App does by day."""
    k = PLINTH / 200
    # the tallest building reaches near the top of the picture; the others stand in proportion to their floors
    room = GROUND - 70 - (spr["c"] + spr["b"] - spr["py"]) * k
    reps = max(0, round(floors / max(most, 1) * room / (spr["s"] * k)))
    pieces = [Image.open(IMG / f"{key}-{p}.webp").convert("RGBA") for p in ("c", "s", "b")]
    w = round(spr["w"] * k)
    crown, strip, base = (im.resize((w, max(1, round(im.size[1] * k)))) for im in pieces)
    h = crown.size[1] + reps * strip.size[1] + base.size[1]
    out = Image.new("RGBA", (w, h))
    y = h - base.size[1]
    out.alpha_composite(base, (0, y))
    for _ in range(reps):
        y -= strip.size[1]
        out.alpha_composite(strip, (0, y))
    out.alpha_composite(crown, (0, y - crown.size[1]))
    if dark:
        rgb = ImageEnhance.Brightness(out.convert("RGB")).enhance(.55)
        rgb.putalpha(out.getchannel("A"))
        out = rgb
    plinth_y = h - base.size[1] + round(spr["py"] * k)  # the plinth's middle, where it meets the ground
    return out, plinth_y


def render(buildings: list[dict]) -> bytes | None:
    """buildings: [{key, floors, dark}]. JPEG bytes, or None when Pillow or the art is missing."""
    try:
        from PIL import Image, ImageDraw, ImageEnhance
        sprites = json.loads((IMG / "spr.json").read_text())
    except Exception:  # noqa: BLE001 - the picture is a bonus; the text still goes out
        log.warning("postcard: Pillow or the art is missing, sending text only")
        return None
    try:
        img = _sky(Image).convert("RGBA")
        d = ImageDraw.Draw(img)
        d.rectangle((0, GROUND - 14, W, H), fill=(217, 222, 232))
        d.rectangle((0, GROUND - 14, W, GROUND - 11), fill=(139, 92, 246))
        drawn = [b for b in buildings if b["key"] in sprites and b["floors"] > 0]
        if not drawn:
            return None
        most = max(b["floors"] for b in drawn)
        # tallest in the middle, so the skyline peaks in the centre of the picture
        drawn.sort(key=lambda b: b["floors"])
        order = drawn[::2] + drawn[1::2][::-1]
        step = min(PLINTH * .92, (W - 80 - PLINTH) / max(len(order) - 1, 1))
        x0 = (W - (step * (len(order) - 1) + PLINTH)) / 2
        # every other tower stands a little in front, so the row reads as depth; the back ones go first
        for i, b in sorted(enumerate(order), key=lambda ib: ib[0] % 2):
            tower, py = _tower(Image, ImageEnhance, b["key"], sprites[b["key"]], b["floors"], most, b.get("dark", False))
            x = round(x0 + i * step)
            y = GROUND + 10 * (i % 2) - py
            img.alpha_composite(tower, (x, max(-tower.size[1] + 40, y)))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=86)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        log.exception("postcard: drawing failed")
        return None
