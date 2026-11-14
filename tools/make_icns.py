"""يولّد assets/pdf2md.icns من assets/logo.png (أيقونة ماك).

يُشغَّل على أي جهاز: icns صيغة تُبنى على ويندوز أيضاً، فلماذا نحتاج ماك لبنائها.
الأحجام القياسية: 16، 32، 64، 128، 256، 512، 1024 (الأصل 512 بكسل).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "logo.png"
DST = ROOT / "assets" / "pdf2md.icns"

SIZES = [(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512)]


def main() -> None:
    im = Image.open(SRC).convert("RGBA")
    base = im if im.width == im.height else im.crop((0, 0, min(im.width, im.height),
                                                      min(im.width, im.height)))
    images = [base.resize(s, Image.LANCZOS) for s in SIZES]
    base.save(DST, format="ICNS", append_images=images)
    print(f"كُتبت الأيقونة: {DST} ({DST.stat().st_size} بايت)")


if __name__ == "__main__":
    main()
