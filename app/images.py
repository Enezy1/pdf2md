"""الصور داخل المستند (ليست الصفحة المصوّرة كلها): علامة [صورة] مكانها، ووصفها عبر المواقع.

- الكشف: صور مضمّنة في الـ PDF تغطي بين 3% و85% من الصفحة (الأصغر شعارات وأختام، والأكبر صفحة
  ممسوحة ضوئياً تُقرأ بـ OCR).
- الوضع الداخلي: "> [صورة]" فقط. وضع "عبر المواقع": تُقصّ الصورة وتُرسل لنموذج رؤية عبر cloud_gate
  فيصفها، وإن كانت لقطة محادثة ينسخ الرسائل ويميّز بين الطرفين.
- النتيجة تُحفظ في بيانات الصفحة (photos) فلا تُعاد عند الاستئناف.
"""

from __future__ import annotations

import re

MIN_AREA, MAX_AREA = 0.03, 0.85
MIN_SIDE = 60  # نقطة

CHAT_RULES = """إن كانت لقطة محادثة (واتساب أو رسائل نصية أو غيرها):
- اكتب كل رسالة في سطر مستقل بالترتيب من الأعلى للأسفل، ولا تحذف أي رسالة.
- ميّز المتحدثَين: "**صاحب الهاتف:**" للفقاعات الملوّنة (الخضراء في واتساب)، و"**الطرف الآخر:**"
  للفقاعات البيضاء أو الرمادية. إن ظهر اسم الطرف الآخر أو رقمه أعلى المحادثة فاستخدمه بدل "الطرف الآخر".
- ضع الوقت أو التاريخ بين قوسين بعد الرسالة إن ظهر. الصور والملصقات والرسائل الصوتية: [صورة] [ملصق] [رسالة صوتية].
- انسخ النص حرفياً كما هو (حتى العامية والأخطاء)."""

PHOTO_PROMPT = f"""هذه صورة مضمّنة داخل مستند. أعد Markdown فقط بلا مقدمات:
{CHAT_RULES}
  وابدأ بسطر "[صورة: محادثة بين …]".
وإن لم تكن محادثة: سطر واحد "[صورة: وصف موجز ودقيق بالعربية لما تُظهره]"، وإن كان فيها نص مهم فانسخه
حرفياً في الأسطر التالية.
لا تخمّن ما لا يظهر بوضوح. اكتب كل الأرقام بالأرقام الإنجليزية 0-9."""

CHAT_PAGE_RULES = "\n\nThis document consists of chat screenshots. Apply these rules (write them in Arabic):\n" + CHAT_RULES


def find_photos(page) -> list[tuple[float, float, float, float]]:
    """مستطيلات الصور المضمّنة التي تستحق علامة/وصفاً، بترتيب ظهورها من الأعلى."""
    area = abs(page.rect) or 1
    out = []
    try:
        infos = page.get_image_info()
    except Exception:
        return []
    for info in infos:
        x0, y0, x1, y1 = info.get("bbox") or (0, 0, 0, 0)
        w, h = x1 - x0, y1 - y0
        if w < MIN_SIDE or h < MIN_SIDE:
            continue
        r = (w * h) / area
        if MIN_AREA <= r <= MAX_AREA:
            out.append((round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)))
    out = sorted(set(out), key=lambda b: (b[1], -b[0]))
    return out


def photo_block(photo: dict) -> str:
    """Markdown مكان الصورة: الوصف إن وُجد، وإلا [صورة]."""
    desc = (photo.get("desc") or "").strip()
    if not desc:
        return "> [صورة]"
    desc = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", desc).strip()
    return "\n".join("> " + ln if ln.strip() else ">" for ln in desc.splitlines())
