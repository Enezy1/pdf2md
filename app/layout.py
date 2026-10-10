"""كشف الصفحات التي يفشل فيها OCR.space فتُقرأ كاملة بنموذج الرؤية: جداول بشبكة خطوط، وصفحات مدوّرة.

الدليل (محضر ميدان الرماية، 2026-10-10): OCR.space بعثر جدولاً مدوّراً تماماً ونقل خلايا جدول عادي إلى غير
أعمدتها، بينما قرأهما نموذج الرؤية صحيحاً. والمصحّح بالتعديلات لا يستطيع إعادة بناء جدول.

الكشف بالصورة نفسها قبل أي إرسال: خطوط أفقية طويلة (40% من العرض) وخطوط عمودية (10% من الارتفاع).
النص العربي متصل فيصنع خطوطاً أفقية قصيرة، لذلك الأفقي يُقاس بالطويل والعمودي هو الدليل الأقوى.
"""

from __future__ import annotations

import io
import re

TABLE_H_LINES = 4      # خطوط أفقية طويلة على الأقل
TABLE_V_LINES = 3      # وخطوط عمودية على الأقل
_ORIENT_RE = re.compile(r"<!--\s*orientation\s*:\s*(\d+)\s*-->")


def _runs(mask, k: int):
    import numpy as np
    c = np.cumsum(np.pad(mask.astype(np.int32), ((0, 0), (1, 0))), axis=1)
    return ((c[:, k:] - c[:, :-k]) >= k * 0.92).any(axis=1)


def _count(flags) -> int:
    n, prev = 0, False
    for v in flags:
        if v and not prev:
            n += 1
        prev = bool(v)
    return n


def grid_lines(jpeg: bytes) -> tuple[int, int]:
    """(خطوط أفقية طويلة، خطوط عمودية) في صورة الصفحة."""
    import numpy as np
    from PIL import Image, ImageFilter
    g = Image.open(io.BytesIO(jpeg)).convert("L")
    g.thumbnail((1000, 1000))
    g = g.filter(ImageFilter.MinFilter(3))       # تسميك الخطوط الرفيعة في المسح الضوئي
    a = np.array(g) < 170
    return _count(_runs(a, int(a.shape[1] * 0.4))), _count(_runs(a.T, int(a.shape[0] * 0.1)))


def has_table(jpeg: bytes) -> bool:
    try:
        h, v = grid_lines(jpeg)
    except Exception:
        return False
    return h >= TABLE_H_LINES and v >= TABLE_V_LINES


def rotation(ocr_text: str) -> int:
    """زاوية الدوران التي أبلغ عنها OCR.space (detectOrientation)، 0 إن لم توجد."""
    m = _ORIENT_RE.search(ocr_text or "")
    return int(m.group(1)) % 360 if m else 0


def unverified_numbers(vision_text: str, ocr_text: str) -> list[str]:
    """الأرقام (خانتان فأكثر) في ناتج الرؤية التي لا تظهر في نص OCR — تُعلَّم للمراجعة بدل الثقة بها."""
    from .facts import normalize_for_compare
    seen = set(re.findall(r"\d{2,}", normalize_for_compare(ocr_text or "")))
    out = []
    for n in re.findall(r"\d{2,}", normalize_for_compare(vision_text or "")):
        if n not in seen and n not in out:
            out.append(n)
    return out


def is_sideways(jpeg: bytes) -> bool:
    """صفحة مدوّرة 90°/270° (جدول بالعرض مثلاً): أسطر النص المتتابعة تصنع تذبذباً قوياً في مجموع كل صف من
    البكسلات عندما تكون الصفحة معتدلة، وفي مجموع كل عمود عندما تكون مدوّرة. OCR.space لا يبلغ دائماً عن
    الدوران (محضر ميدان الرماية ص3: قرأها وأعاد TextOrientation=0 وبعثر الجدول).
    جُرّب على 13 صفحة حقيقية و9 صفحات عيّنات: لا خطأ."""
    import numpy as np
    from PIL import Image
    try:
        g = Image.open(io.BytesIO(jpeg)).convert("L")
        g.thumbnail((800, 800))
        a = (np.array(g) < 150).astype(float)
        h, w = a.shape
        a = a[int(h * .08):int(h * .92), int(w * .08):int(w * .92)]
        if a.mean() < 0.003:     # صفحة شبه فارغة
            return False

        def energy(p):
            return float(np.abs(np.diff(p)).mean() / max(p.mean(), 1e-6))
        if not energy(a.mean(0)) > energy(a.mean(1)) * SIDEWAYS_RATIO:
            return False
        # دليل ثانٍ مستقل: الفراغات بين الأسطر. في الصفحة المعتدلة صفوف فارغة كثيرة وأعمدة فارغة قليلة،
        # وفي المدوّرة العكس. (الأول وحده خُدع بصفحة اختبار أسطرها متطابقة فتتحاذى كلماتها عمودياً.)
        ys, xs = np.nonzero(a)
        y0, y1 = np.percentile(ys, [3, 97]).astype(int)
        x0, x1 = np.percentile(xs, [3, 97]).astype(int)
        box = a[y0:y1 + 1, x0:x1 + 1]
        return (box.mean(0) < 0.01).mean() >= (box.mean(1) < 0.01).mean()
    except Exception:
        return False


SIDEWAYS_RATIO = 1.0
