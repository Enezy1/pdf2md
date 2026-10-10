"""يولّد ملف PDF تجريبي (غير سري) يحتوي كل أنواع الصفحات لاختبار البرنامج.

    python tools/make_sample_pdf.py [مجلد_الإخراج]

الصفحات:
 1. نص عربي حقيقي (طبقة نص سليمة)
 2. صفحة مصوّرة (صورة فقط)
 3. صفحة فارغة
 4. صفحة مصوّرة فوقها طبقة OCR مخفية من "الماسح"
 5. طبقة نص تالفة (رموز بلا حروف) => يجب أن تُعامل كمصوّرة
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf as fitz

FONTS = fitz.Archive("C:/Windows/Fonts")
CSS = "@font-face {font-family: ar; src: url(arial.ttf);} p {font-family: ar; font-size: 15px; line-height: 1.6;}"
RECT = fitz.Rect(60, 70, 535, 780)

TEXT = """
<p dir="rtl">عقد إيجار — نموذج تجريبي غير حقيقي</p>
<p dir="rtl">إنه في يوم الأحد الموافق 2025/03/16 تم الاتفاق بين الطرف الأول السيد أحمد علي (المؤجر)
والطرف الثاني السيدة فاطمة حسن (المستأجر) على ما يلي:</p>
<p dir="rtl">البند الأول: يؤجر الطرف الأول للطرف الثاني الشقة الكائنة في شارع النخيل مقابل مبلغ قدره
750 دينار شهرياً تدفع مقدماً في بداية كل شهر.</p>
<p dir="rtl">البند الثاني: مدة العقد سنة واحدة تبدأ من تاريخ التوقيع وتتجدد تلقائياً ما لم يخطر أحد الطرفين الآخر كتابياً.</p>
"""


def _text_page(doc: fitz.Document, html: str) -> fitz.Page:
    page = doc.new_page()
    page.insert_htmlbox(RECT, html, css=CSS, archive=FONTS)
    return page


def _image_of(html: str, dpi: int = 150) -> bytes:
    tmp = fitz.open()
    _text_page(tmp, html)
    png = tmp[0].get_pixmap(dpi=dpi).tobytes("png")
    tmp.close()
    return png


def build(path: Path) -> Path:
    doc = fitz.open()
    _text_page(doc, TEXT)                                              # 1
    doc.new_page().insert_image(fitz.Rect(0, 0, 595, 842), stream=_image_of(TEXT))  # 2
    doc.new_page()                                                     # 3
    p4 = doc.new_page()                                                # 4
    p4.insert_image(fitz.Rect(0, 0, 595, 842), stream=_image_of(TEXT))
    p4.insert_text((60, 100), "hidden scanner OCR layer " * 6, render_mode=3)
    p5 = doc.new_page()                                                # 5
    p5.insert_text((60, 100), "\ue001\ue002\ue003 \ue004\ue005 " * 20)  # حروف خاصة بلا معنى
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


# ---------------------------------------------------------------- جداول عربية (من اليمين لليسار)
CELL_CSS = "@font-face {font-family: ar; src: url(arial.ttf);} p {font-family: ar; font-size: 11px; margin: 0;}"
TABLE_HEADER = ["م", "البيان", "التاريخ", "المبلغ (د.ك)"]       # من اليمين لليسار
TABLE_WIDTHS = [30, 190, 90, 100]
TABLE_ROWS_1 = [["١", "قسط الإيجار الأول", "2025/03/16", "1,250.500"],
                ["2", "رسوم الخدمة السنوية", "15/04/2025", "٣٠٠"],
                ["3", "غرامة تأخير", "01-05-2025", "75.250"]]
TABLE_ROWS_2 = [["4", "قسط الإيجار الثاني", "2025/06/16", "1,250.500"],
                ["5", "تأمين مسترد", "2025/07/01", "500"]]


def rtl_table(page: fitz.Page, top: float, rows: list[list[str]], widths: list[float],
              right: float = 535, row_h: float = 24) -> float:
    """يرسم جدولاً بخطوط، العمود الأول في rows هو الأيمن بصرياً. يعيد أسفل الجدول."""
    y = top
    for row in rows:
        x = right
        for text, w in zip(row, widths):
            r = fitz.Rect(x - w, y, x, y + row_h)
            page.draw_rect(r, color=(0, 0, 0), width=0.7)
            page.insert_htmlbox(r + (4, 5, -4, -2), f'<p dir="rtl" style="text-align:right">{text}</p>',
                                css=CELL_CSS, archive=FONTS)
            x -= w
        y += row_h
    return y


def build_tables(path: Path) -> Path:
    """صفحة 1: فقرة ثم جدول عربي ينتهي في آخر الصفحة. صفحة 2: تكملة الجدول بترويسة مكررة،
    ثم فقرة. صفحة 3: جدول بعدد أعمدة مختلف (3)."""
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_htmlbox(fitz.Rect(60, 60, 535, 120), '<p dir="rtl">كشف الدفعات المستحقة على المستأجر</p>',
                      css=CSS, archive=FONTS)
    rtl_table(p1, 130, [TABLE_HEADER] + TABLE_ROWS_1, TABLE_WIDTHS)
    p1.insert_text((290, 820), "1", fontsize=9)                       # رقم صفحة
    p2 = doc.new_page()
    bottom = rtl_table(p2, 60, [TABLE_HEADER] + TABLE_ROWS_2, TABLE_WIDTHS)
    p2.insert_htmlbox(fitz.Rect(60, bottom + 20, 535, bottom + 80),
                      '<p dir="rtl">وقد تم سداد جميع الدفعات المذكورة أعلاه.</p>', css=CSS, archive=FONTS)
    p3 = doc.new_page()
    rtl_table(p3, 60, [["الطرف", "الصفة", "العنوان"], ["أحمد علي", "مؤجر", "الكويت"],
                       ["فاطمة حسن", "مستأجر", "حولي"]], [120, 120, 150])
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


if __name__ == "__main__":
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "samples"
    print(build(out_dir / "sample_mixed.pdf"))
    print(build_tables(out_dir / "sample_tables.pdf"))
