"""needs_human_review.md: كل صفحة تحتاج نظرة إنسان، في كل الملفات.

لكل صفحة: اسم الملف، رقمها، الأسباب، ومسار صورتها في <output>/review/. التقرير محلي فقط.
البيانات المجمّعة في <output>/.state/review.json (لكل ملف حسب بصمته)، والتقرير يُعاد توليده
منها بالكامل عند تحديث أي ملف، فلا تبقى فيه صفحات حُلّت.
في وضع "عبر المواقع" تظهر صفحاتها الضعيفة أيضاً في نافذة المراجعة (pipeline.review_items).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pymupdf as fitz

from . import checkpoint as cp
from .fsutil import atomic_write_json, read_json, write_markdown_file
from .quality import WEAK_REASONS

log = logging.getLogger(__name__)

REPORT_NAME = "needs_human_review.md"
REVIEW_DIR = "review"
IMAGE_DPI = 110

STATUS_REASONS = {
    cp.NEEDS_REVIEW: "بانتظار قرارك في نافذة المراجعة",
    cp.UNRELIABLE: "ناتج المحرّك غير موثوق (لم يُكتب نصه)",
    cp.ERROR: "تعذّرت المعالجة",
}
# الصفحات المصوّرة التي لم تُقرأ بعد (بلا محرّك) لا تُعد "ضعيفة": تظهر في حالة الاكتمال في INDEX.md.


def _label(code: str) -> str:
    return WEAK_REASONS.get(code, code)


def file_entries(state: cp.FileState, mismatch_pages: list[int]) -> list[dict]:
    """الصفحات التي تحتاج مراجعة في ملف واحد، مع أسبابها (رموز + نص)."""
    out = []
    for n in range(1, state.page_count + 1):
        p = state.page(n) or {}
        codes: list[str] = []
        texts: list[str] = []
        status = p.get("status")
        if status in STATUS_REASONS:
            codes.append(status)
            texts.append(STATUS_REASONS[status])
            if p.get("review_reason"):
                codes.append(p["review_reason"])
                texts.append(_label(p["review_reason"]))
        sc = p.get("score") or {}
        if sc.get("weak"):
            for c in sc.get("reasons", []):
                codes.append(c)
                texts.append(_label(c))
            texts[-1] += f" (الدرجة {sc.get('score')})"
        if (p.get("ocr_check") or {}).get("ok") is False and status == cp.DONE:
            codes.append("weak_match")
            texts.append(_label("weak_match"))
        low = (p.get("paddle") or {}).get("low_lines")
        if low and status == cp.DONE and p.get("method") == "ocr:paddle":
            codes.append("paddle_low_conf")
            texts.append(_label("paddle_low_conf") + f" — الأسطر {', '.join(map(str, low))}")
        if (p.get("paddle") or {}).get("poor_digits") and status == cp.DONE and p.get("method") == "ocr:paddle":
            codes.append("poor_scan_digits")
            texts.append(_label("poor_scan_digits"))
        if n in mismatch_pages:
            codes.append("table_mismatch")
            texts.append(_label("table_mismatch"))
        if codes:
            out.append({"page": n, "codes": list(dict.fromkeys(codes)), "reasons": list(dict.fromkeys(texts))})
    return out


def _image_name(source_name: str, file_hash: str, n: int) -> str:
    return f"{Path(source_name).stem}__{file_hash[:8]}_p{n:04d}.png"


def update_file(output_dir: Path, state: cp.FileState, source_name: str, doc: fitz.Document | None,
                mismatch_pages: list[int]) -> list[dict]:
    """يحدّث مدخل الملف ويعيد توليد التقرير. يعيد مدخلات هذا الملف."""
    output_dir = Path(output_dir)
    entries = file_entries(state, mismatch_pages)
    review_dir = output_dir / REVIEW_DIR
    keep = set()
    for e in entries:
        name = _image_name(source_name, state.file_hash, e["page"])
        img = review_dir / name
        keep.add(name)
        if doc is not None and not img.exists():
            try:
                review_dir.mkdir(parents=True, exist_ok=True)
                doc.load_page(e["page"] - 1).get_pixmap(dpi=IMAGE_DPI, alpha=False).save(str(img))
            except Exception as ex:
                log.warning("تعذّر حفظ صورة المراجعة للصفحة %d: %s", e["page"], ex)
        e["image"] = f"{REVIEW_DIR}/{name}" if img.exists() else ""
    # صور صفحات حُلّت لهذا الملف تُحذف
    if review_dir.exists():
        prefix = f"{Path(source_name).stem}__{state.file_hash[:8]}_p"
        for f in review_dir.glob(prefix + "*.png"):
            if f.name not in keep:
                f.unlink(missing_ok=True)
    db_path = output_dir / ".state" / "review.json"
    try:
        db = read_json(db_path, default={}) or {}
    except (json.JSONDecodeError, OSError):
        db = {}
    if entries:
        db[state.file_hash] = {"name": source_name, "pages": state.page_count, "entries": entries}
    else:
        db.pop(state.file_hash, None)
    atomic_write_json(db_path, db)
    write_report(output_dir, db)
    return entries


def write_report(output_dir: Path, db: dict) -> Path:
    files = sorted(db.values(), key=lambda f: f["name"])
    total = sum(len(f["entries"]) for f in files)
    out = ["# صفحات تحتاج مراجعة", "",
           f"<!-- review-report | files:{len(files)} | pages:{total} -->", ""]
    if not files:
        out.append("لا توجد صفحات تحتاج مراجعة.")
    for f in files:
        out += [f"## {f['name']}", "", f"{len(f['entries'])} صفحة من {f['pages']}", "",
                "| الصفحة | الأسباب | الصورة |", "| --- | --- | --- |"]
        for e in f["entries"]:
            img = f"[{e['image']}]({e['image'].replace(' ', '%20')})" if e.get("image") else "—"
            reasons = "؛ ".join(e["reasons"]).replace("|", "\\|")
            out.append(f"| {e['page']} | {reasons} | {img} |")
        out.append("")
    target = Path(output_dir) / REPORT_NAME
    write_markdown_file(target, "\n".join(out).rstrip() + "\n")
    return target


def pending_count(output_dir: Path) -> int:
    """عدد الصفحات في needs_human_review.md (للواجهة)."""
    try:
        db = read_json(Path(output_dir) / ".state" / "review.json", default={}) or {}
    except (json.JSONDecodeError, OSError):
        return 0
    return sum(len(f.get("entries", [])) for f in db.values() if isinstance(f, dict))
