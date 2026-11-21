"""حالة الملف في جدول الملفات: تُخزَّن بيانات (حالة + عدّاد)، ويُبنى منها نص الشارة ولونها.

لماذا بيانات لا نص جاهز: النص المركّب «تم · 28 للمراجعة» كان معناه يحتاج تحليل النص
(`status.startswith("تم")`)، وكان ملتبساً بعينه: «جاهز» كان بالأخضر كأنه «تم»، و«تم» رمادياً،
والرقم بلا وحدة (٢٨ صفحة معلّقة أم ٢٨ صفحة مراجَعة؟). الآن الحالة قيمة من (queued…error)،
والنص واللون مشتقّان منها، فيمكن تغيير الصياغة دون أن يمسّ منطق البرنامج.

  queued   ← فُحص الملف وعُرف عدد صفحاته، وينتظر دوره (لا بدأ التحويل)
  scanning ← يقرأ الملف ليعرف عدد صفحاته
  working  ← يجري التحويل الآن
  partial  ← تحويل سابق متبقٍّ (يُستكمل من حيث وقف)
  done     ← انتهى التحويل (قد تبقى صفحات معلّقة للمراجعة)
  error    ← فشل
"""

from __future__ import annotations

from dataclasses import dataclass

QUEUED = "queued"
SCANNING = "scanning"
WORKING = "working"
PARTIAL = "partial"
DONE = "done"
ERROR = "error"


@dataclass
class FileStatus:
    state: str = QUEUED
    done: int = 0        # صفحات منجزة
    total: int = 0       # إجمالي الصفحات المطلوب
    pending: int = 0     # صفحات معلّقة تنتظر المراجعة
    error: str = ""      # سبب الفشل


def pages_label(n: int) -> str:
    """عدد الصفحات بصيغة عربية سليمة: «صفحة واحدة» / «صفحتان» / «3 صفحات» / «28 صفحة»."""
    if n == 1:
        return "صفحة واحدة"
    if n == 2:
        return "صفحتان"
    if 3 <= n <= 10:
        return f"{n} صفحات"
    return f"{n} صفحة"


def pending_label(n: int) -> str:
    """الصفحات المعلّقة التي تنتظر المراجعة: «28 صفحة معلّقة»."""
    if n == 1:
        return "صفحة واحدة معلّقة"
    if n == 2:
        return "صفحتان معلّقتان"
    if 3 <= n <= 10:
        return f"{n} صفحات معلّقات"
    return f"{n} صفحة معلّقة"


def text(st: FileStatus) -> str:
    """نص شارة الحالة: كلمة حالة واحدة، وبعدها عدّاد اختياري لا أكثر.

    العدّاد بالكلمات («أنجز 50 من 100») لا بالنسبة المائلة («50/100»): الأرقام المائلة
    تنقلب بصرياً في نص من اليمين لليسار فيصير «4/171» كأنها «171/4».
    """
    if st.state == ERROR:
        return "خطأ"
    if st.state == SCANNING:
        return "جارٍ الفحص"
    if st.state == WORKING:
        return f"جارٍ · أنجز {st.done} من {st.total}" if st.total else "جارٍ"
    if st.state == PARTIAL:
        return f"مكتمل جزئياً · أنجز {st.done} من {st.total}" if st.total else "مكتمل جزئياً"
    if st.state == DONE:
        return f"تم · {pending_label(st.pending)}" if st.pending else "تم"
    return "بانتظار البدء"


def tone(st: FileStatus) -> str:
    """لون الشارة: ok (نظيف) · warn (يحتاج إجراء) · busy (يعمل) · err · "" (رمادي)."""
    if st.state == ERROR:
        return "err"
    if st.state in (SCANNING, WORKING):
        return "busy"
    if st.state == PARTIAL:
        return "warn"
    if st.state == DONE:
        return "warn" if st.pending else "ok"
    return ""


def note(st: FileStatus, limit: int = 46) -> str:
    """سطر توضيح تحت اسم الملف: سبب الخطأ، أو ما تبقّى من تحويل سابق.

    قصير عمداً: السطر يوسّع عمود الاسم ولا يمكن تمريره أفقياً (الجدول في عمود تمرير رأسي فقط).
    """
    if st.state == ERROR:
        return _clip(st.error, limit)
    if st.state == PARTIAL:
        return _clip(f"أنجز {st.done} من {st.total} — سيُستكمل", limit)
    if st.state == DONE and st.pending:
        return _clip("راجعها من زر المراجعة أسفل النافذة", limit)
    return ""


def explain(st: FileStatus) -> str:
    """شرح الحالة بجملة واحدة (للشرح الشامل في السجل أو التلميح)."""
    if st.state == ERROR:
        return f"تعذّر إتمام الملف: {st.error}" if st.error else "تعذّر إتمام الملف"
    if st.state == SCANNING:
        return "يقرأ الملف ليعرف عدد صفحاته"
    if st.state == WORKING:
        return f"يجري التحويل — أنجز {st.done} من {st.total} صفحة"
    if st.state == PARTIAL:
        return f"توقّف التحويل سابقاً بعد {st.done} صفحة — سيُستكمل"
    if st.state == DONE:
        return (f"انتهى التحويل — {pending_label(st.pending)} تنتظر المراجعة" if st.pending
                else "انتهى التحويل كاملاً")
    return "جاهز للبدء — لم يبدأ التحويل بعد"


def _clip(s: str, limit: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"
