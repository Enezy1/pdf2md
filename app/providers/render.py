"""رسم الصفحة للمزوّدات الخارجية: JPEG مضغوط بدقة قابلة للضبط وحد أقصى للأبعاد."""

from __future__ import annotations

import io

import pymupdf as fitz
from PIL import Image

from .base import PageImage

MAX_BYTES = 3_500_000  # يقلّل الجودة ثم الأبعاد حتى يصبح الحجم مقبولاً لكل المزوّدات


def ink_ratio(img: Image.Image) -> float:
    """نسبة البكسلات الداكنة (تقدير لكثافة النص في الصفحة)."""
    g = img.convert("L")
    g.thumbnail((400, 400))
    hist = g.histogram()
    dark = sum(hist[:160])
    return dark / max(1, g.width * g.height)


def _encode(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def render_page_jpeg(page: fitz.Page, n: int, dpi: int = 150, max_dim: int = 2000, quality: int = 80,
                     clip: tuple[float, float, float, float] | None = None) -> PageImage:
    kw = {"dpi": dpi, "alpha": False}
    if clip:
        kw["clip"] = fitz.Rect(*clip)
    pix = page.get_pixmap(**kw)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    data = _encode(img, quality)
    while len(data) > MAX_BYTES and (quality > 50 or max(img.size) > 1000):
        if quality > 50:
            quality -= 10
        else:
            img.thumbnail((int(img.width * 0.8), int(img.height * 0.8)), Image.LANCZOS)
        data = _encode(img, quality)
    return PageImage(n, data, dpi, mime="image/jpeg", ink=ink_ratio(img))


def page_ink(page: fitz.Page) -> float:
    pix = page.get_pixmap(dpi=40, alpha=False)
    return ink_ratio(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))


def probe_image() -> PageImage:
    """صفحة وهمية يولّدها البرنامج لاختبار الاتصال — لا مستند حقيقي أبداً."""
    from PIL import ImageDraw
    img = Image.new("RGB", (800, 400), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 60), "PDF2MD CONNECTION TEST", fill="black")
    d.text((40, 120), "Page 1 - 12345", fill="black")
    d.rectangle((40, 200, 760, 360), outline="black")
    d.text((60, 260), "TEST TABLE | A | B", fill="black")
    return PageImage(1, _encode(img, 80), 72, mime="image/jpeg", ink=ink_ratio(img))
