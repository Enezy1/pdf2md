"""ملخص كل ملف في الفهرس (3 إلى 5 أسطر)، بثلاثة مصادر يظهر اسمها في الفهرس:

- "محلي": ملخص استخراجي بحت يعمل على النص الناتج (لا صور، لا نموذج، لا شبكة). يختار أهم
  الجمل بتكرار الكلمات المهمة، بعد تجاهل: الأسطر القصيرة جداً، الترويسات/التذييلات المتكررة
  بين الصفحات، أرقام الصفحات، والجداول. كل سطر يحمل رقم صفحته (ص N) للإسناد.
- "أنماط": جمل مركّبة من الوقائع المستخرجة (facts.py) إذا لم يكفِ النص لملخص استخراجي.
- "API": في وضع "عبر المواقع" فقط، عبر cloud_gate.send_text (خيار معطّل افتراضياً).
  أي فشل ⇒ يرجع للملخص المحلي. في الوضع الداخلي لا يصل شيء لهذا المسار، والبوابة ترفضه أيضاً.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass

from .facts import Fact, find_amounts, find_case_numbers, find_dates

log = logging.getLogger(__name__)

SOURCE_PATTERNS = "أنماط"
SOURCE_LOCAL = "محلي"
SOURCE_API = "API"

MIN_LINE_CHARS = 25        # الأسطر الأقصر تُتجاهل (عناوين، تواقيع، أرقام)
REPEAT_SHARE = 0.3         # سطر يتكرر في 30% من الصفحات أو أكثر (وعلى صفحتين على الأقل) = ترويسة/تذييل
MAX_SENT_CHARS = 240
API_MAX_CHARS = 30_000

_PAGE_NUM = re.compile(r"^\W*(?:صفحة|الصفحة|ص|page)?\s*[\d٠-٩۰-۹]{1,4}(?:\s*(?:من|/|of)\s*[\d٠-٩۰-۹]{1,4})?\W*$", re.I)
_DIAC = re.compile(r"[ً-ٰٟـ]")
_WORD = re.compile(r"[ء-يA-Za-z]{3,}")
_STOPWORDS = set("""
في من على إلى الى عن مع أن ان إن كان كانت التي الذي الذين هذا هذه ذلك تلك وقد قد لم لن ما لا هو هي هم
كما أو او ثم حيث بين عند كل بعد قبل حتى غير عليه عليها منه منها فيه فيها به بها له لها وذلك وفي ومن وعلى
وهو وهي وأن وان بأن بان لدى أي أيضا ايضا وكذلك وكان وكانت يكون تكون السيد السيدة رقم تاريخ بتاريخ
""".split())


@dataclass
class Summary:
    lines: list[str]
    source: str


def _norm_line(s: str) -> str:
    return re.sub(r"[\d٠-٩۰-۹]+", "#", re.sub(r"\s+", " ", _DIAC.sub("", s))).strip()


_OWN_NOTE = re.compile(r"^_\(.*\)_$")   # أسطر يكتبها البرنامج: _(تكملة جدول …)_ / _(صفحة فارغة)_


def clean_pages(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """[(رقم الصفحة، فقرة)] بعد حذف التعليقات والجداول وأرقام الصفحات وأسطر البرنامج، والأسطر
    المتكررة بين الصفحات (ترويسات/تذييلات)، ثم الأسطر القصيرة جداً. الفقرات تفصلها أسطر فارغة."""
    per_page: list[tuple[int, list[list[str]]]] = []
    for n, md in pages:
        t = re.sub(r"<!--.*?-->", " ", md or "", flags=re.S)
        paras: list[list[str]] = [[]]
        for ln in t.splitlines():
            s = ln.strip()
            if not s:
                paras.append([])
                continue
            if s.startswith("|") or _PAGE_NUM.match(s) or _OWN_NOTE.match(s):
                continue
            s = re.sub(r"^[#>*\-+\s]+|[*_`]+", "", s).strip()
            if s:
                paras[-1].append(s)
        per_page.append((n, [p for p in paras if p]))
    counts = Counter(k for _, ps in per_page for k in {_norm_line(x) for p in ps for x in p})
    n_pages = len([1 for _, ps in per_page if ps])
    repeated = {k for k, c in counts.items() if n_pages >= 2 and c >= 2 and c / n_pages >= REPEAT_SHARE}
    out = []
    for n, ps in per_page:
        for p in ps:
            kept = [s for s in p if _norm_line(s) not in repeated and len(s) >= MIN_LINE_CHARS]
            if kept:
                out.append((n, " ".join(kept)))
    return out


def _sentences(lines: list[tuple[int, str]]) -> list[tuple[int, int, str]]:
    """[(الترتيب، الصفحة، الجملة)]. الأسطر المتتالية من نفس الصفحة تُجمع ثم تُقسَّم عند . ؟ ! ؛"""
    out: list[tuple[int, int, str]] = []
    buf: list[str] = []
    cur = None

    def flush():
        if not buf:
            return
        text = " ".join(buf)
        for part in re.split(r"(?<=[.؟!?؛])\s+", text):
            part = part.strip()
            while len(part) > MAX_SENT_CHARS:  # جمل قانونية طويلة: قطع عند فاصلة
                cut = part.rfind("،", 0, MAX_SENT_CHARS)
                cut = cut if cut > 60 else MAX_SENT_CHARS
                out.append((len(out), cur, part[:cut].strip(" ،") + "…"))
                part = part[cut:].strip(" ،")
            if len(part) >= MIN_LINE_CHARS:
                out.append((len(out), cur, part))
    for n, s in lines:   # كل فقرة تُقسَّم وحدها
        flush()
        buf, cur = [s], n
    flush()
    return out


def _words(s: str) -> list[str]:
    return [w for w in _WORD.findall(_DIAC.sub("", s)) if w not in _STOPWORDS]


def extractive_summary(pages: list[tuple[int, str]], max_lines: int = 5, min_lines: int = 3) -> list[str]:
    sents = _sentences(clean_pages(pages))
    if not sents:
        return []
    tf = Counter(w for _, _, s in sents for w in _words(s))
    if not tf:
        return []
    top = max(tf.values())
    first_page = sents[0][1]
    scored = []
    for i, n, s in sents:
        ws = _words(s)
        if len(ws) < 4:
            continue
        sc = sum(tf[w] / top for w in ws) / math.sqrt(len(ws))
        if find_dates(s) or find_amounts(s) or find_case_numbers(s):
            sc *= 1.3
        if n == first_page:
            sc *= 1.2
        scored.append((sc, i, n, s, set(ws)))
    scored.sort(key=lambda x: -x[0])
    total_words = sum(tf.values())
    k = max_lines if total_words >= 1500 else min_lines
    chosen = []
    for sc, i, n, s, ws in scored:
        if any(len(ws & c[4]) / max(1, len(ws | c[4])) > 0.6 for c in chosen):
            continue
        chosen.append((sc, i, n, s, ws))
        if len(chosen) >= k:
            break
    chosen.sort(key=lambda c: c[1])
    return [f"{s} (ص {n})" for _, _, n, s, _ in chosen]


def pattern_summary(doc_type: str, facts: list[Fact], page_count: int) -> list[str]:
    def vals(kind: str, k: int = 3) -> list[Fact]:
        return [f for f in facts if f.kind == kind][:k]

    def pg(f: Fact) -> str:
        return "ص " + "، ".join(map(str, f.pages[:3]))

    lines = [f"مستند من نوع: {doc_type}، عدد صفحاته {page_count}."]
    parties = vals("party") + vals("entity", 2)
    if parties:
        lines.append("الأطراف والجهات: " + "؛ ".join(f"{f.value} ({pg(f)})" for f in parties) + ".")
    cases = vals("case")
    if cases:
        lines.append("أرقام القضايا: " + "؛ ".join(f"{f.value} ({pg(f)})" for f in cases) + ".")
    dates = sorted({f.value for f in facts if f.kind == "date"})
    if dates:
        lines.append(f"التواريخ الواردة: من {dates[0]} إلى {dates[-1]} ({len(dates)} تاريخ).")
    amounts = vals("amount", 3)
    if amounts:
        lines.append("مبالغ: " + "؛ ".join(f"{f.value} ({pg(f)})" for f in amounts) + ".")
    return lines[:5]


API_PROMPT = (
    "لخّص المستند التالي بالعربية في 3 إلى 5 أسطر، كل سطر يبدأ بـ - . اذكر نوع المستند وأطرافه وموضوعه "
    "وأهم الطلبات أو النتائج. لا تضف أي معلومة غير موجودة في النص، ولا تغيّر الأرقام أو المبالغ أو التواريخ. "
    "علامات <!-- page:N --> تدل على أرقام الصفحات؛ أضف (ص N) في نهاية كل سطر. "
    "اكتب كل الأرقام بالأرقام الإنجليزية 0-9 دائماً."
)


def api_text(pages: list[tuple[int, str]]) -> str:
    out, size = [], 0
    for n, md in pages:
        body = re.sub(r"<!--(?!\s*page).*?-->", " ", md or "", flags=re.S).strip()
        chunk = f"<!-- page:{n} -->\n{body}\n"
        if size + len(chunk) > API_MAX_CHARS:
            break
        out.append(chunk)
        size += len(chunk)
    return "\n".join(out)


class ApiSummarizer:
    """تلخيص عبر مزوّد خارجي. كل طلب يمر بـ gate.open_file (بصمة من القرص + فحص الوضع)
    ثم gate.send_text (إعادة الفحص + سجل الإرسال)."""

    def __init__(self, gate, provider, model: str = ""):
        self.gate = gate
        self.provider = provider
        self.model = model

    def summarize(self, path, file_hash: str, pages: list[tuple[int, str]]) -> list[str]:
        ticket = self.gate.open_file(path)          # يرفع CloudBlockedError في الوضع الداخلي
        if ticket.file_hash != file_hash:
            raise PermissionError("بصمة الملف على القرص لا تطابق الحالة المحفوظة")
        text = self.gate.send_text(ticket, self.provider, API_PROMPT, api_text(pages), self.model)
        lines = [re.sub(r"^\s*[-*•]\s*", "", ln).strip() for ln in (text or "").splitlines()]
        return [ln for ln in lines if ln][:5]


def summarize_file(pages: list[tuple[int, str]], doc_type: str, facts: list[Fact], page_count: int,
                   api: ApiSummarizer | None = None, api_allowed: bool = False, path=None,
                   file_hash: str = "") -> Summary:
    """api_allowed يحسبه المستدعي من gate.allowed؛ والبوابة تتحقق مجدداً عند الإرسال."""
    if api is not None and api_allowed and path is not None:
        try:
            lines = api.summarize(path, file_hash, pages)
            if len(lines) >= 2:
                return Summary(lines, SOURCE_API)
        except Exception as e:
            log.warning("تعذّر التلخيص عبر API، سيُستخدم المحلي: %s", type(e).__name__)
    lines = extractive_summary(pages)
    if len(lines) >= 2:
        return Summary(lines, SOURCE_LOCAL)
    return Summary(pattern_summary(doc_type, facts, page_count), SOURCE_PATTERNS)
