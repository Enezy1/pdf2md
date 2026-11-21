"""يولّد assets/pdf2md.icns من assets/logo.png (أيقونة ماك).

يُشغَّل على أي جهاز: icns صيغة تُبنى على ويندوز أيضاً، فلماذا نحتاج ماك لبنائها.
الأحجام القياسية: 16، 32، 64، 128، 256، 512، 1024 (الأصل 512 بكسل).

مثل أيقونة ويندوز (tools/make_ico.py): نقصّ الشارة على حدود الرسم الفعلي قبل
التصغير، فتملأ الإطار كاملاً بدل أن تسبقه هوامش شفافة.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "logo.png"
DST = ROOT / "assets" / "pdf2md.icns"

SIZES = [(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512)]


def artwork(im: Image.Image) -> Image.Image:
    """يقصّ الصورة على حدود الرسم الفعلي (يزيل الهامش الشفاف)."""
    if im.width != im.height:
        side = min(im.width, im.height)
        im = im.crop((0, 0, side, side))
    bbox = im.getchannel("A").getbbox()
    return im.crop(bbox) if bbox else im


def main() -> None:
    im = Image.open(SRC).convert("RGBA")
    art = artwork(im)
    images = [art.resize(s, Image.LANCZOS) for s in SIZES]
    base = images[-1]  # 512 — المحور الذي تكتب منه باق الأحجام
    base.save(DST, format="ICNS", append_images=images[:-1])
    print(f"كُتبت الأيقونة: {DST} ({DST.stat().st_size} بايت)")


if __name__ == "__main__":
    main()
