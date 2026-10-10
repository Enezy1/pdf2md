"""INDEX.md: فهرس كل الملفات المعالجة في مجلد الإخراج.

الصيغة (تصلح للإنسان ولقراءة نموذج لاحقاً):
  1. سطر واحد لكل ملف سهل المسح: الاسم، الصفحات، النوع، حالة الاكتمال، رابط ملف الـ Markdown.
  2. ثم قسم لكل ملف: الملخص (ومصدره: أنماط/محلي/API)، الأسماء، أرقام القضايا، التواريخ، المبالغ —
     وكل واقعة معها أرقام الصفحات التي وردت فيها (ص N) للإسناد.
بيانات الإسناد كاملة (بلا حد للعدد) في .state/index.json.

التحديث تدريجي: لكل ملف توقيع (بصمته + وقت آخر تحديث لحالته + نسخة الفهرس + مصدر الملخص)،
ويُعاد بناء مدخله فقط إذا تغيّر التوقيع. full=True يعيد بناء الكل.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from . import checkpoint as cp
from .config import Settings
from .facts import detect_doc_type, extract_facts
from .fsutil import atomic_write_json, read_json, write_markdown_file
from .markdown_out import merged_fragments
from .summarize import ApiSummarizer, summarize_file

log = logging.getLogger(__name__)

INDEX_VERSION = 1
INDEX_NAME = "INDEX.md"
MAX_LIST = 25

_HASH_FILE = re.compile(r"^[0-9a-f]{64}\.json$")

KIND_TITLES = [
    ("party", "الأطراف"),
    ("lawyer", "المحامون والوكلاء"),
    ("entity", "الجهات"),
    ("case", "أرقام القضايا"),
    ("date", "التواريخ"),
    ("amount", "المبالغ"),
]


def load_states(state_dir: Path) -> list[cp.FileState]:
    """آخر حالة لكل اسم ملف (إذا تغيّر الملف يبقى له حالة قديمة ببصمة أخرى: نأخذ الأحدث)."""
    latest: dict[str, tuple[str, cp.FileState]] = {}
    if not state_dir.exists():
        return []
    for f in state_dir.iterdir():
        if not _HASH_FILE.match(f.name):
            continue
        try:
            data = read_json(f) or {}
            st = cp.FileState(state_dir, data["file_hash"], data["source_name"], data["page_count"])
        except (json.JSONDecodeError, OSError, KeyError, TypeError) as e:
            log.warning("تجاهل حالة غير مقروءة %s: %s", f.name, e)
            continue
        key = st.data["source_name"]
        upd = st.data.get("updated", st.data.get("created", ""))
        if key not in latest or upd > latest[key][0]:
            latest[key] = (upd, st)
    return [st for _, st in sorted(latest.values(), key=lambda x: x[1].data["source_name"])]


def completeness(state: cp.FileState) -> dict:
    c = state.counts()
    unread = sum(c.get(k, 0) for k in (cp.ILLEGIBLE, cp.UNRELIABLE, cp.PENDING_ENGINE, cp.ERROR, "todo"))
    review = c.get(cp.NEEDS_REVIEW, 0)
    weak = sum(1 for n in range(1, state.page_count + 1)
               if ((state.page(n) or {}).get("score") or {}).get("weak"))
    return {"pages": state.page_count, "done": c.get(cp.DONE, 0), "blank": c.get(cp.BLANK, 0),
            "needs_review": review, "unread": unread, "weak": weak, "complete": unread == 0 and review == 0,
            "counts": c}


def _signature(state: cp.FileState, use_api: bool) -> str:
    """بصمة محتوى الحالة: حالة كل صفحة + حجم/وقت تعديل جزئها (لا يكفي وقت التحديث بدقة الثانية)."""
    h = hashlib.sha1(json.dumps(state.data.get("pages", {}), sort_keys=True, ensure_ascii=False).encode("utf-8"))
    for n in range(1, state.page_count + 1):
        try:
            st = state.fragment_path(n).stat()
            h.update(f"{n}:{st.st_size}:{st.st_mtime_ns};".encode())
        except OSError:
            pass
    return f"{INDEX_VERSION}|{state.file_hash}|{h.hexdigest()[:16]}|{'api' if use_api else 'local'}"


def build_entry(state: cp.FileState, api: ApiSummarizer | None, api_allowed: bool) -> dict:
    merged, tstats = merged_fragments(state)
    pages = [(n, t) for n, t in sorted(merged.items()) if t.strip()]
    doc_type, _scores = detect_doc_type(pages[:3])
    facts = extract_facts(pages)
    summary = summarize_file(pages, doc_type, facts, state.page_count, api=api, api_allowed=api_allowed,
                             path=state.data.get("source_path"), file_hash=state.file_hash)
    name = state.data["source_name"]
    return {
        "name": name,
        "sha256": state.file_hash,
        "markdown": f"docs/{Path(name).stem}.md",
        "doc_type": doc_type,
        "completeness": completeness(state),
        "tables": tstats.to_dict(),
        "summary": {"source": summary.source, "lines": summary.lines},
        "facts": [f.to_dict() for f in facts],
    }


def _status_text(c: dict) -> str:
    if c["complete"]:
        s = "✓ مكتمل"
    else:
        parts = []
        if c["needs_review"]:
            parts.append(f"{c['needs_review']} بانتظار المراجعة")
        if c["unread"]:
            parts.append(f"{c['unread']} غير مقروءة")
        s = "⚠ ناقص: " + "، ".join(parts)
    if c.get("weak"):
        s += f" (صفحات ضعيفة: {c['weak']})"
    return s


def _pages(p: list[int]) -> str:
    return "ص " + "، ".join(map(str, p[:12])) + ("…" if len(p) > 12 else "")


def _link(path: str) -> str:
    return f"[{path}]({path.replace(' ', '%20')})"


def render_index(entries: list[dict]) -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = ["# فهرس المستندات", "",
           f"<!-- index v{INDEX_VERSION} | files: {len(entries)} | generated: {now} -->",
           "> سطر لكل ملف: الاسم — الصفحات — النوع — حالة الاكتمال — ملف الـ Markdown. "
           "التفاصيل تحته، وكل واقعة معها أرقام صفحاتها (ص N).", ""]
    for e in entries:
        c = e["completeness"]
        out.append(f"- **{e['name']}** — {c['pages']} ص — {e['doc_type']} — {_status_text(c)} — {_link(e['markdown'])}")
    for e in entries:
        c = e["completeness"]
        out += ["", "---", "", f"## {e['name']}", "",
                f"<!-- file: {e['name']} | sha256: {e['sha256']} | pages: {c['pages']} | type: {e['doc_type']} "
                f"| complete: {'yes' if c['complete'] else 'no'} | summary: {e['summary']['source']} -->",
                f"- عدد الصفحات: {c['pages']}",
                f"- نوع المستند: {e['doc_type']}",
                f"- الحالة: {_status_text(c)}",
                f"- ملف الـ Markdown: {_link(e['markdown'])}"]
        t = e.get("tables") or {}
        if t.get("detected"):
            out.append(f"- الجداول: {t['detected']} (مدموجة: {t['merged']}، تضارب أعمدة: {t['mismatched']})")
        out += ["", f"### الملخص (المصدر: {e['summary']['source']})", ""]
        out += [f"- {ln}" for ln in e["summary"]["lines"]] or ["- (لا يوجد نص كافٍ للتلخيص)"]
        for kind, title in KIND_TITLES:
            items = [f for f in e["facts"] if f["kind"] == kind]
            if not items:
                continue
            out += ["", f"### {title}", ""]
            for f in items[:MAX_LIST]:
                shown = f["value"]
                if kind in ("date", "amount") and f["raw"] and f["raw"] != f["value"]:
                    shown += f" (كما ورد: {f['raw']})"
                out.append(f"- {shown} — {_pages(f['pages'])}")
            if len(items) > MAX_LIST:
                out.append(f"- … و{len(items) - MAX_LIST} أخرى (القائمة الكاملة في .state/index.json)")
    return "\n".join(out).rstrip() + "\n"


def build_index(output_dir: Path, settings: Settings, full: bool = False,
                api: ApiSummarizer | None = None) -> dict:
    """يبني/يحدّث INDEX.md. api يُمرَّر فقط إذا كان التلخيص عبر API مفعّلاً ومزوّد متاح؛
    وفي الوضع الداخلي لا يُرسل شيء (gate.allowed هنا، ثم open_file/send_text في البوابة)."""
    output_dir = Path(output_dir)
    state_dir = output_dir / ".state"
    db_path = state_dir / "index.json"
    try:
        db = {} if full else (read_json(db_path, default={}) or {})
    except (json.JSONDecodeError, OSError):
        db = {}
    if db.get("version") != INDEX_VERSION:
        db = {"version": INDEX_VERSION, "files": {}}
    old = db.get("files", {})
    new: dict[str, dict] = {}
    built = reused = 0
    for state in load_states(state_dir):
        use_api = bool(settings.summary_via_api and api is not None and api.gate.allowed(state.file_hash))
        sig = _signature(state, use_api)
        prev = old.get(state.file_hash)
        if prev and prev.get("sig") == sig:
            new[state.file_hash] = prev
            reused += 1
            continue
        try:
            entry = build_entry(state, api if use_api else None, use_api)
        except Exception as e:
            log.exception("تعذّر بناء مدخل الفهرس للملف %s", state.data.get("source_name"))
            entry = {"name": state.data["source_name"], "sha256": state.file_hash, "doc_type": "غير محدد",
                     "markdown": f"docs/{Path(state.data['source_name']).stem}.md",
                     "completeness": completeness(state), "tables": {},
                     "summary": {"source": "—", "lines": [f"تعذّر بناء المدخل: {type(e).__name__}"]}, "facts": []}
            sig = ""  # يُعاد في المرة القادمة
        entry["sig"] = sig
        new[state.file_hash] = entry
        built += 1
    removed = len(set(old) - set(new))
    db["files"] = new
    atomic_write_json(db_path, db)
    entries = sorted(new.values(), key=lambda e: e["name"])
    target = output_dir / INDEX_NAME
    write_markdown_file(target, render_index(entries), settings.normalize_digits)
    return {"path": str(target), "built": built, "reused": reused, "removed": removed, "files": len(entries)}
