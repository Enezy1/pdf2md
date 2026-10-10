"""الجداول: استخراجها من طبقة النص، ودمج الجداول الممتدة بين الصفحات.

الاستخراج:
- find_tables تُستخدم لتحديد الشبكة (حدود الخلايا) فقط. نص الخلايا نستخرجه بأنفسنا لأن
  extract() تُخرج العربية بترتيبها البصري المقلوب (الحروف والكلمات).
- ترتيب الأعمدة: find_tables تعيدها من اليسار لليمين. في الجدول العربي نقلبها حتى يكون
  أول عمود في الـ Markdown هو العمود الأيمن في الـ PDF، وهو نفس ترتيب القراءة ونفس ما
  تُخرجه النماذج البصرية (فيصح الدمج بين صفحة نصية وصفحة OCR).
- أي شك (أعمدة غير متسقة، خلايا فارغة كثيرة، نص داخل الجدول لم يدخل الخلايا) ⇒ لا جدول،
  ويُستخرج النص كفقرات كما كان.
- النص داخل الخلايا يُنقل حرفياً: لا تُعاد صياغة الأرقام أو المبالغ أو التواريخ أبداً.

الدمج: انظر merge_tables.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import pymupdf as fitz

log = logging.getLogger(__name__)

TABLE_CONT = "<!-- table-continues -->"
MISMATCH_TAG = "check:table_mismatch"

_ARABIC = re.compile(r"[ء-يٱ-ۓﭐ-﷿ﹰ-ﻼ]")
_LATIN = re.compile(r"[A-Za-z]")
_SEP_ROW = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_PAGE_NUM = re.compile(r"^\W*(?:صفحة|ص|page)?\s*[\d٠-٩۰-۹]{1,4}(?:\s*(?:من|/|of)\s*[\d٠-٩۰-۹]{1,4})?\W*$", re.I)

# حدود "الجدول المعقول"
MAX_EMPTY_FRACTION = 0.4
MIN_COVERAGE = 0.9        # نسبة حروف منطقة الجدول التي دخلت الخلايا
MAX_CELL_CHARS = 600


@dataclass
class PageTable:
    bbox: fitz.Rect
    rows: list[list[str]]
    rtl: bool

    @property
    def markdown(self) -> str:
        return rows_to_markdown(self.rows)


# ---------------------------------------------------------------- الاستخراج
def rows_to_markdown(rows: list[list[str]]) -> str:
    def cell(s: str) -> str:
        return re.sub(r"\s+", " ", s).replace("|", "\\|").strip()

    out = ["| " + " | ".join(cell(c) for c in rows[0]) + " |",
           "| " + " | ".join("---" for _ in rows[0]) + " |"]
    out += ["| " + " | ".join(cell(c) for c in r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _page_lines(page: fitz.Page) -> list[tuple[fitz.Rect, list[tuple[fitz.Rect, str]]]]:
    """كل سطر من طبقة النص مع حروفه بترتيب التدفق (الترتيب المنطقي) ومستطيل كل حرف."""
    out = []
    raw = page.get_text("rawdict")
    for block in raw["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            chars = [(fitz.Rect(ch["bbox"]), ch["c"]) for span in line["spans"] for ch in span["chars"]]
            if chars:
                out.append((fitz.Rect(line["bbox"]), chars))
    return out


def _center_in(r: fitz.Rect, box: fitz.Rect) -> bool:
    cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
    return box.x0 <= cx <= box.x1 and box.y0 <= cy <= box.y1


class VisualOrderError(ValueError):
    """نص عربي مخزّن بترتيبه البصري (من اليسار لليمين): لا نقلبه، نرفض الجدول."""


def _visual_order(chars: list[tuple[fitz.Rect, str]]) -> bool:
    ar = [r.x0 for r, c in chars if _ARABIC.match(c)]
    if len(ar) < 3:
        return False
    inc = sum(1 for a, b in zip(ar, ar[1:]) if b > a)
    return inc / (len(ar) - 1) > 0.8


def _cell_text(lines, box: fitz.Rect) -> str:
    """نص الخلية: حروف كل سطر داخل الخلية بترتيبها المنطقي، والأسطر من أعلى لأسفل.
    القطع المتجاورة على نفس الارتفاع تُرتَّب من اليمين في النص العربي (كما في pdf_io)."""
    from .pdf_io import fix_mirrored_brackets, normalize_arabic
    pieces: list[tuple[fitz.Rect, str]] = []
    for _lr, chars in lines:
        inside = [(r, c) for r, c in chars if _center_in(r, box)]
        if not inside:
            continue
        if _visual_order(inside):
            raise VisualOrderError("ترتيب بصري")
        rect = fitz.Rect(inside[0][0])
        for r, _ in inside[1:]:
            rect |= r
        pieces.append((rect, "".join(c for _, c in inside)))
    if not pieces:
        return ""
    # تجميع حسب الارتفاع
    groups: list[list[tuple[fitz.Rect, str]]] = []
    for rect, text in sorted(pieces, key=lambda p: p[0].y0):
        for g in groups:
            gr = g[0][0]
            if min(gr.y1, rect.y1) - max(gr.y0, rect.y0) > 0.5 * min(gr.height, rect.height):
                g.append((rect, text))
                break
        else:
            groups.append([(rect, text)])
    out = []
    for g in groups:
        joined = " ".join(t for _, t in g)
        rtl = len(_ARABIC.findall(joined)) >= len(_LATIN.findall(joined))
        g.sort(key=lambda it: -it[0].x1 if rtl else it[0].x0)
        out.append(" ".join(t for _, t in g))
    return fix_mirrored_brackets(re.sub(r"\s+", " ", normalize_arabic(" ".join(out))).strip())


def _chars_in(lines, box: fitz.Rect) -> int:
    return sum(1 for _lr, chars in lines for r, c in chars if not c.isspace() and _center_in(r, box))


def is_plausible(rows: list[list[str]]) -> bool:
    if len(rows) < 2 or len(rows[0]) < 2:
        return False
    width = len(rows[0])
    if any(len(r) != width for r in rows):
        return False
    cells = [c for r in rows for c in r]
    if sum(1 for c in cells if not c.strip()) / len(cells) > MAX_EMPTY_FRACTION:
        return False
    if any(not any(r[i].strip() for r in rows) for i in range(width)):
        return False  # عمود فارغ بالكامل = شبكة خاطئة
    if any(len(c) > MAX_CELL_CHARS for c in cells):
        return False
    if not any(c.strip() for c in rows[0]):
        return False
    return sum(1 for r in rows if any(c.strip() for c in r)) >= 2


def extract_tables(page: fitz.Page, rtl_columns: bool = True) -> list[PageTable]:
    """الجداول الموثوقة فقط في الصفحة. أي خطأ ⇒ قائمة فارغة (يرجع النص العادي)."""
    try:
        found = page.find_tables()
    except Exception as e:  # نسخ PyMuPDF القديمة أو صفحة غريبة
        log.info("find_tables فشلت: %s", e)
        return []
    if not found.tables:
        return []
    lines = _page_lines(page)
    out: list[PageTable] = []
    for t in found.tables:
        try:
            rows: list[list[str]] = []
            for row in t.rows:
                rows.append([_cell_text(lines, fitz.Rect(c)) if c else "" for c in row.cells])
            bbox = fitz.Rect(t.bbox)
            if not is_plausible(rows):
                continue
            total = _chars_in(lines, bbox)
            used = sum(len(re.sub(r"\s", "", c)) for r in rows for c in r)
            if total and used / total < MIN_COVERAGE:
                continue  # نص داخل منطقة الجدول خارج الخلايا (خلايا مدموجة؟)
            text = " ".join(c for r in rows for c in r)
            rtl = len(_ARABIC.findall(text)) >= len(_LATIN.findall(text))
            if rtl and rtl_columns:
                rows = [list(reversed(r)) for r in rows]
            out.append(PageTable(bbox, rows, rtl))
        except Exception as e:
            log.info("تجاهل جدول غير مفهوم: %s", e)
    return out


# ---------------------------------------------------------------- تحليل Markdown
def split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def _norm_row(cells: list[str]) -> str:
    return "|".join(re.sub(r"\s+", " ", c).strip() for c in cells)


def _is_table_line(line: str) -> bool:
    return line.strip().startswith("|")


def _is_filler(line: str) -> bool:
    s = line.strip()
    return not s or (s.startswith("<!--") and s.endswith("-->")) or bool(_PAGE_NUM.match(s))


def count_tables(markdown: str) -> int:
    """عدد الجداول (كتلة أسطر | فيها سطر فاصل ---)."""
    n, in_table, has_sep = 0, False, False
    for line in markdown.splitlines() + [""]:
        if _is_table_line(line):
            in_table = True
            has_sep = has_sep or bool(_SEP_ROW.match(line.strip()))
        else:
            if in_table and has_sep:
                n += 1
            in_table = has_sep = False
    return n


@dataclass
class _Block:
    start: int
    end: int  # بعد آخر سطر


def _leading_table(lines: list[str]) -> tuple[_Block | None, int | None]:
    """الجدول في أول الصفحة (بعد أسطر فارغة/تعليقات/رقم صفحة)، وموضع علامة table-continues."""
    marker = None
    i = 0
    while i < len(lines) and not _is_table_line(lines[i]):
        if lines[i].strip() == TABLE_CONT:
            marker = i
        elif not _is_filler(lines[i]):
            return None, marker
        i += 1
    if i >= len(lines):
        return None, marker
    j = i
    while j < len(lines) and _is_table_line(lines[j]):
        j += 1
    return _Block(i, j), marker


def _trailing_table(lines: list[str]) -> _Block | None:
    j = len(lines)
    while j > 0 and not _is_table_line(lines[j - 1]):
        if not _is_filler(lines[j - 1]):
            return None
        j -= 1
    if j == 0:
        return None
    i = j
    while i > 0 and _is_table_line(lines[i - 1]):
        i -= 1
    return _Block(i, j)


def _data_rows(lines: list[str]) -> list[list[str]]:
    return [split_row(ln) for ln in lines if not _SEP_ROW.match(ln.strip())]


@dataclass
class MergeStats:
    detected: int = 0
    merged: int = 0
    mismatched: int = 0
    mismatch_pages: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"detected": self.detected, "merged": self.merged, "mismatched": self.mismatched,
                "mismatch_pages": list(self.mismatch_pages)}


@dataclass
class _Anchor:
    page: int
    start: int
    end: int
    cols: int
    header: str


def merge_tables(pages: list[tuple[int, str, str]]) -> tuple[dict[int, str], MergeStats]:
    """pages: [(رقم الصفحة، نصها، طريقة الاستخراج)] بالترتيب. الصفحات غير المكتملة تُمرَّر بنص فارغ.

    يُعتبر جدول أول الصفحة امتداداً لجدول آخر الصفحة السابقة إذا:
      - بدأت الصفحة بالعلامة <!-- table-continues --> (من OCR/API)، أو
      - الصفحتان من طبقة النص، والسابقة تنتهي بجدول، والحالية تبدأ بجدول.
    عند تطابق عدد الأعمدة تُنقل الصفوف إلى جدول الصفحة الأولى وتُحذف الترويسة المكررة.
    عند عدم التطابق مع وجود العلامة: لا دمج، ويُعلَّم الموضع <!-- check:table_mismatch -->.
    (بدون العلامة لا دليل على الامتداد، فلا تعليم.)
    """
    frags = {n: text.splitlines() for n, text, _ in pages}
    methods = {n: m for n, _, m in pages}
    stats = MergeStats()
    anchor: _Anchor | None = None
    prev_n = None
    for n, text, method in pages:
        stats.detected += count_tables(text)
        lines = frags[n]
        if not text.strip() or (prev_n is not None and n != prev_n + 1):
            anchor = None
        lead, marker = _leading_table(lines)
        continued = False
        if lead and anchor:
            heuristic = methods.get(anchor.page) == "text_layer" and method == "text_layer"
            if marker is not None or heuristic:
                rows = _data_rows(lines[lead.start:lead.end])
                if rows and _norm_row(rows[0]) == anchor.header:
                    rows = rows[1:]  # ترويسة مكررة
                if rows and all(len(r) == anchor.cols for r in rows):
                    target = frags[anchor.page]
                    new = ["| " + " | ".join(r) + " |" for r in rows]
                    target[anchor.end:anchor.end] = new
                    anchor.end += len(new)
                    target.insert(anchor.start, f"<!-- table-merged: +{len(rows)} rows from page {n} -->")
                    anchor.start += 1
                    anchor.end += 1
                    drop_from = marker if marker is not None and marker < lead.start else lead.start
                    lines[drop_from:lead.end] = [
                        f"<!-- table-continued-from:{anchor.page} rows={len(rows)} -->",
                        f"_(تكملة جدول صفحة {anchor.page} — نُقلت صفوفه إلى هناك)_",
                    ]
                    stats.merged += 1
                    continued = True
                elif marker is not None:
                    got = sorted({len(r) for r in rows}) if rows else [0]
                    lines.insert(lead.start, f"<!-- {MISMATCH_TAG} cols={anchor.cols}/{','.join(map(str, got))} "
                                             f"prev_page={anchor.page} -->")
                    stats.mismatched += 1
                    stats.mismatch_pages.append(n)
        if continued and all(_is_filler(ln) or ln.startswith("_(تكملة جدول") for ln in lines):
            pass  # الصفحة كلها تكملة: الجدول ما زال مفتوحاً على الصفحة الأولى
        else:
            trail = _trailing_table(lines)
            anchor = None
            if trail:
                rows = _data_rows(lines[trail.start:trail.end])
                if rows and len({len(r) for r in rows}) == 1:
                    anchor = _Anchor(n, trail.start, trail.end, len(rows[0]), _norm_row(rows[0]))
        prev_n = n
    return {n: "\n".join(ls) for n, ls in frags.items()}, stats
