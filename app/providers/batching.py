"""تقسيم الصفحات إلى دفعات صغيرة، وتعليمات النسخ الحرفي، وفحص الاكتمال بعد كل دفعة.

النماذج تهمل الوسط في المستندات الطويلة، لذلك:
- كل طلب 1 أو 2 أو 3 أو 5 صفحات (مقيّد بحد المزوّد)، وكل صفحة صورة منفصلة.
- كل صفحة يجب أن تبدأ بالعلامة <!-- page:N --> بالرقم الذي نحدده نحن.
- بعد كل دفعة: كل العلامات المطلوبة موجودة، بالترتيب، بلا علامات غريبة، ولا صفحة
  فارغة أو قصيرة بشكل مريب مقارنة بكثافة حبرها.
- إذا فشل الفحص تُعاد الدفعة صفحةً صفحة؛ والصفحة المفردة الفاشلة تُعلَّق للمراجعة.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from ..quality import looks_like_refusal, repetition_problem, suspiciously_short
from ..digits import DIGITS_RULE
from .base import OcrRequest, PageImage

MARKER_RE = re.compile(r"<!--\s*page\s*:\s*(\d+)\s*-->")
TABLE_CONT = "<!-- table-continues -->"
_FENCE_RE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*?)\n\s*```\s*$", re.S)


def chunk_pages(pages: list[int], size: int) -> list[list[int]]:
    """دفعات من صفحات متتالية فقط (صفحة مفصولة لا تُضم لدفعة غيرها)."""
    out: list[list[int]] = []
    for p in pages:
        if out and len(out[-1]) < size and out[-1][-1] == p - 1:
            out[-1].append(p)
        else:
            out.append([p])
    return out


def plan_whole(pages: list[int], cap: int) -> list[list[int]]:
    """المستند كله في طلب واحد حتى cap صفحة؛ فوقه أجزاء متساوية (41–80 ⇒ جزآن). بلا دمج ولا إعادة تقسيم."""
    if not pages:
        return []
    parts = -(-len(pages) // max(1, cap))
    size = -(-len(pages) // parts)
    return [pages[i:i + size] for i in range(0, len(pages), size)]


def build_prompt(pages: list[int], previous_tail: str = "") -> str:
    nums = ", ".join(str(p) for p in pages)
    lines = [
        f"You will receive {len(pages)} image(s), each one a separate page of a document. Page numbers: {nums}.",
        "Transcribe EVERY page into Markdown, verbatim. Rules:",
        "- Copy the full text of each page exactly as written. Do NOT summarize, shorten, skip, translate or comment.",
        "- The text is mostly Arabic: keep the words and their letter order exactly.",
        "- Do NOT correct spelling, grammar or hamza even if the original looks wrong: copy every word exactly as "
        "printed (e.g. keep \"كاتلوج\" and \"هناك\" as written, do not change them to \"كتالوج\" or \"هنالك\").",
        "- Arabic tables: the FIRST Markdown column must be the RIGHTMOST column of the table as read in Arabic "
        "(also when the page is rotated). Keep every cell value, including row numbers.",
        "- Use Markdown headings for titles and Markdown tables for tables.",
        "- Start each page with its marker on its own line, using exactly these numbers in this order: "
        + " ".join(f"<!-- page:{p} -->" for p in pages),
        "- If a page has no text at all, still output its marker followed by <!-- blank -->.",
        f"- If a page starts with the continuation of a table from the previous page, put {TABLE_CONT} on the line "
        "after the page marker and continue the table rows without repeating the header.",
        "- " + DIGITS_RULE,
        "- Output only the Markdown, no code fences, no explanations.",
    ]
    if previous_tail.strip():
        lines += [
            "",
            "For context only (do NOT transcribe it), these are the last lines of the page before the first image:",
            "<<<",
            previous_tail.strip(),
            ">>>",
        ]
    return "\n".join(lines)


@dataclass
class CheckResult:
    ok: bool
    pages: dict[int, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    bad: set[int] = field(default_factory=set)     # صفحات فشلت (مفقودة/فارغة/قصيرة/مكررة)


def split_and_check(text: str, images: list[PageImage]) -> CheckResult:
    m = _FENCE_RE.match(text or "")
    if m:
        text = m.group(1)
    expected = [img.page_number for img in images]
    ink = {img.page_number: img.ink for img in images}
    found = [(int(mm.group(1)), mm.start(), mm.end()) for mm in MARKER_RE.finditer(text or "")]
    if not found and len(images) == 1 and (text or "").strip():
        # صفحة واحدة والنموذج كتب النص بلا علامة: النص كله لهذه الصفحة (العلامة شكلية هنا، لا لبس في نسبته)
        text = f"<!-- page:{images[0].page_number} -->\n{text.strip()}"
        found = [(images[0].page_number, 0, text.index("\n"))]
    res = CheckResult(ok=True)
    nums = [n for n, _, _ in found]
    if nums != expected:
        missing = [p for p in expected if p not in nums]
        extra = [p for p in nums if p not in expected]
        res.bad.update(missing)
        if missing:
            res.problems.append(f"علامات مفقودة: {missing}")
        if extra:
            res.problems.append(f"علامات غير مطلوبة: {extra}")
        if not missing and not extra:
            res.problems.append("العلامات بغير الترتيب أو مكررة")
        res.ok = False
    for i, (n, _s, e) in enumerate(found):
        end = found[i + 1][1] if i + 1 < len(found) else len(text)
        body = text[e:end].strip()
        if n in ink and n not in res.pages:
            res.pages[n] = body
    for p in expected:
        body = res.pages.get(p, "")
        is_blank = body.strip() == "<!-- blank -->"
        if p in res.pages and (is_blank or not body) and (ink.get(p) or 0) >= 0.006:
            res.ok = False
            res.bad.add(p)
            res.problems.append(f"صفحة {p} فارغة في الناتج")
        elif p in res.pages and not is_blank and suspiciously_short(body, ink.get(p)):
            res.ok = False
            res.bad.add(p)
            res.problems.append(f"صفحة {p} قصيرة بشكل مريب")
        elif p in res.pages and looks_like_refusal(body.replace("<!-- blank -->", "")):
            res.ok = False
            res.bad.add(p)
            res.problems.append(f"صفحة {p}: النموذج اعتذر عن القراءة")
        elif p in res.pages and (rep := repetition_problem(body)):
            res.ok = False
            res.bad.add(p)
            res.problems.append(f"صفحة {p}: {rep}")
    return res


@dataclass
class PageOutcome:
    page: int
    markdown: str | None            # None ⇒ تُعلَّق للمراجعة
    batch_size: int
    problems: list[str] = field(default_factory=list)
    partial: str = ""               # أفضل نص وصل لهذه الصفحة رغم فشل الفحص (قصير/ناقص): يُقبل بدل التعليق


Send = Callable[[str, OcrRequest], str]


def run_with_split(send: Send, images: list[PageImage], previous_tail: str = "",
                   on_retry: Callable[[list[int], list[str]], None] | None = None,
                   extra_rules: str = "") -> list[PageOutcome]:
    """يرسل الدفعة؛ عند فشل الفحص يعيدها صفحةً صفحة. أخطاء المزوّد (429، الحصة...) تُرفع للأعلى."""
    pages = [img.page_number for img in images]
    text = send(build_prompt(pages, previous_tail) + extra_rules, OcrRequest(images, previous_tail))
    chk = split_and_check(text, images)
    if chk.ok:
        return [PageOutcome(p, chk.pages[p], len(images)) for p in pages]
    if len(images) == 1:
        return [PageOutcome(pages[0], None, 1, chk.problems, chk.pages.get(pages[0], ""))]
    if len(images) > BIG_BATCH:
        return _retry_bad(send, images, chk, previous_tail, on_retry, extra_rules)
    if on_retry:
        on_retry(pages, chk.problems)
    out: list[PageOutcome] = []
    tail = previous_tail
    for img in images:
        r = send(build_prompt([img.page_number], tail) + extra_rules, OcrRequest([img], tail))
        c = split_and_check(r, [img])
        md = c.pages.get(img.page_number) if c.ok else None
        out.append(PageOutcome(img.page_number, md, 1, c.problems, c.pages.get(img.page_number, "")))
        tail = tail_lines(md or "", 8)
    return out


BIG_BATCH = 5   # فوقه لا تُعاد الدفعة صفحةً صفحة (يستهلك عشرات الطلبات): تُحفظ السليمة ويُعاد الناقص فقط


def _retry_bad(send: Send, images: list[PageImage], chk: CheckResult, previous_tail: str,
               on_retry, extra_rules: str) -> list[PageOutcome]:
    pages = [img.page_number for img in images]
    bad = {p for p in pages if p in chk.bad or p not in chk.pages}
    if not bad:            # علامات بغير الترتيب فقط: المحتوى مربوط برقم الصفحة فيُقبل
        return [PageOutcome(p, chk.pages[p], len(images)) for p in pages]
    if on_retry:
        on_retry(sorted(bad), chk.problems)
    good = {p: PageOutcome(p, chk.pages[p], len(images)) for p in pages if p not in bad}
    redo = [img for img in images if img.page_number in bad]
    # دفعات صغيرة: الفشل غالباً من طول الرد (الصفحات الأخيرة تُقصَّر أو تُحذف)، فالإعادة في دفعة كبيرة تفشل مثله
    groups = [redo[i:i + BIG_BATCH] for i in range(0, len(redo), BIG_BATCH)]
    for g in groups:
        for o in run_with_split(send, g, previous_tail if g[0] is images[0] else "", on_retry, extra_rules):
            good[o.page] = o
    return [good[p] for p in pages]


def tail_lines(markdown: str, n: int) -> str:
    lines = [ln for ln in markdown.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-n:]) if n > 0 else ""


def normalize_page(markdown: str) -> str:
    return "" if markdown.strip() == "<!-- blank -->" else markdown.strip()
