"""يولّد assets/pdf2md.ico من assets/logo.png (أيقونة ويندوز).

يُشغَّل على أي جهاز: ico صيغة تُبنى على ويندوز نفسها.
الأحجام: 16، 24، 32، 48، 64، 128، 256.

مهمّة القصّ: الشارة في logo.png مرسومة داخل مربّع شفاف (حوافّها 83 بكسل على 512)،
فلو استُعملت الصورة كما هي لظهرت الأيقونة أصغر من neighbouring apps — لأن ودة
أيقونات ويندوز ثابتة (48 بكسل)، والفرق يصير في الفراغ الشفاف حول الرسم. لذلك
نقصّ الصورة على حدود الرسم الفعلي (alpha bbox) قبل التصغير، فتملأ الأيقونة
الإطار كاملاً — تماماً مثل أيقونة «نظام جلسات الخبراء» المجاورة.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "logo.png"
DST = ROOT / "assets" / "pdf2md.ico"

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def artwork(im: Image.Image) -> Image.Image:
    """يقصّ الصورة على حدود الرسم الفعلي (يزيل الهامش الشفاف)."""
    if im.width != im.height:
        side = min(im.width, im.height)
        im = im.crop((0, 0, side, side))
    bbox = im.getchannel("A").getbbox()
    return im.crop(bbox) if bbox else im


def build_ico(frames: list[tuple[tuple[int, int], bytes]]) -> bytes:
    """ico بإطارات PNG (يدعمها ويندوز فيا/7 فما فوق)."""
    out = io.BytesIO()
    out.write(struct.pack("<HHH", 0, 1, len(frames)))
    offset = 6 + 16 * len(frames)
    body = b""
    for size, data in frames:
        out.write(struct.pack("<BBBBHHII", size[0] % 256, size[1] % 256,
                              0, 0, 1, 32, len(data), offset))
        offset += len(data)
        body += data
    out.write(body)
    return out.getvalue()


def main() -> None:
    src = Image.open(SRC).convert("RGBA")
    art = artwork(src)

    frames: list[tuple[tuple[int, int], bytes]] = []
    for size in SIZES:
        img = art.resize(size, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        frames.append((size, buf.getvalue()))

    blob = build_ico(frames)
    DST.write_bytes(blob)
    print(f"كُتبت الأيقونة: {DST} ({len(blob)} بايت)")
    for size, data in frames:
        print(f"  {size[0]:>3}px  {len(data):>6} بايت")


if __name__ == "__main__":
    main()
