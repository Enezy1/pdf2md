"""مختبر النماذج: مقارنة النماذج المحلية على صفحات مختارة لمعرفة الأفضل في العربية.

يُستخدم من تبويب "داخلي" في الإعدادات، ومن سطر الأوامر عبر benchmark.py:

    python benchmark.py "C:\\path\\file.pdf" --pages 2,5,9
    python benchmark.py file.pdf --pages 3 --models qwen3vl-4b,gemma3-4b --out report.html

لكل نموذج مثبّت (GGUF + mmproj متحقق منهما) وكل دقة: زمن كل صفحة، و"درجة الجودة"
(مؤشرات الصفحة الضعيفة نفسها التي يستخدمها البرنامج)، ونسبة الكلمات العربية غير
المعروفة في القاموس المحلي، و"الدقة" مقارنة بنص مرجعي إن وُجد:
- النص المرجعي تلقائياً من طبقة النص إن كانت الصفحة نصية سليمة (أفضل طريقة للقياس:
  اختر PDF نصياً، فالنموذج يقرأ الصورة ونقارنه بالنص الأصلي).
- أو ملف نص يحدده المستخدم (صفحات مفصولة بسطر "=== صفحة N ===").

كل تشغيل يُحفظ في lab/results.jsonl داخل مجلد البيانات لتُقارن النماذج عبر أيام.
التقرير HTML يحتوي صور الصفحات ونصها، فلا تشاركه.

--api: يضيف مزوّدات الـ API المفعّلة للمقارنة. يضبط البوابة على "عبر المواقع"
صراحةً لهذا التشغيل فقط (اختيار المستخدم بالعلم --api).
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import pymupdf as fitz

from . import model_manager as mm
from . import models_catalog as cat
from .config import Settings, app_data_dir
from .crosscheck import match_score
from .pdf_io import PageKind, classify_page, page_text_to_markdown, render_page_png
from .providers.base import EngineUnavailable, OcrRequest, PageImage

MAX_PAGES = 5
DPIS = (150, 200)
_REF_SPLIT = re.compile(r"^\s*===\s*(?:صفحة|page)\s*(\d+)\s*===\s*$", re.M | re.I)


class Stopped(Exception):
    pass


def parse_pages(spec: str, page_count: int) -> list[int]:
    pages = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            pages.extend(range(a, b + 1))
        else:
            pages.append(int(part))
    pages = list(dict.fromkeys(pages))
    if not pages:
        raise ValueError("لم تُحدد صفحات")
    if len(pages) > MAX_PAGES:
        raise ValueError(f"الحد الأقصى {MAX_PAGES} صفحات")
    bad = [p for p in pages if not 1 <= p <= page_count]
    if bad:
        raise ValueError(f"صفحات خارج النطاق 1..{page_count}: {bad}")
    return pages


def select_models(requested: list[str] | None) -> tuple[list[cat.ModelSpec], dict[str, str]]:
    """يعيد (النماذج القابلة للتشغيل، {اسم: سبب الاستبعاد})."""
    excluded = dict(cat.EXCLUDED)
    if requested:
        known = {m.id for m in cat.MODELS}
        for r in requested:
            if r not in known and r != "paddle":
                excluded[r] = "ليس في الكتالوج (لا يوجد GGUF + mmproj متحقق منه)"
        pool = [m for m in cat.MODELS if m.id in requested]
    else:
        pool = list(cat.MODELS)
    runnable = []
    for m in pool:
        model_p, mmproj_p = mm.model_paths(m)
        ok_m, ok_p = mm.is_verified(model_p, m.model), mm.is_verified(mmproj_p, m.mmproj)
        if ok_m and ok_p:
            runnable.append(m)
        else:
            missing = [f.name for f, ok in ((m.model, ok_m), (m.mmproj, ok_p)) if not ok]
            excluded[m.label] = "غير مثبّت أو لم يُتحقق منه: " + "، ".join(missing)
    return runnable, excluded


# ------------------------------------------------------------------ المرجع
def parse_reference_file(path: Path) -> dict[int, str]:
    """ملف نص مرجعي: صفحات مفصولة بسطر "=== صفحة N ===". بدون فواصل = نص صفحة واحدة (رقم 0)."""
    text = Path(path).read_text(encoding="utf-8-sig")
    parts = _REF_SPLIT.split(text)
    if len(parts) == 1:
        return {0: text.strip()}
    out = {}
    for i in range(1, len(parts) - 1, 2):
        out[int(parts[i])] = parts[i + 1].strip()
    return out


def text_layer_reference(pdf: Path, pages: list[int], min_chars: int = 30) -> dict[int, str]:
    """النص الأصلي للصفحات النصية السليمة (لا المصوّرة ولا المعطوبة)."""
    out = {}
    with fitz.open(pdf) as doc:
        for p in pages:
            page = doc[p - 1]
            if classify_page(page, min_chars).kind == PageKind.TEXT:
                out[p] = page_text_to_markdown(page)
    return out


# ------------------------------------------------------------------ التقرير
@dataclass
class Cell:
    text: str = ""
    seconds: float | None = None
    error: str = ""
    quality: float | None = None      # درجة الصفحة 0..1 (مؤشرات الصفحة الضعيفة)
    unknown: float | None = None      # نسبة الكلمات العربية غير المعروفة في القاموس
    accuracy: float | None = None     # التطابق مع النص المرجعي 0..1
    confidence: float | None = None   # PaddleOCR: أدنى ثقة سطر في الصفحة
    low_lines: int | None = None      # PaddleOCR: عدد الأسطر الأقل من العتبة (معلّمة بـ [؟] في النص)


@dataclass
class Report:
    pdf_name: str
    pages: list[int]
    images: dict[int, bytes] = field(default_factory=dict)
    results: dict[tuple[str, int, int], Cell] = field(default_factory=dict)  # (model, dpi, page)
    models: list[cat.ModelSpec] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)
    load_seconds: dict[str, float] = field(default_factory=dict)
    dpis: tuple[int, ...] = DPIS
    reference: dict[int, str] = field(default_factory=dict)
    reference_source: str = ""
    # مزوّدات الـ API: (provider_id, page) → Cell، وحجم الدفعة، والتنبيهات
    api_names: dict[str, str] = field(default_factory=dict)
    api_results: dict[tuple[str, int], Cell] = field(default_factory=dict)
    api_batch: dict[str, int] = field(default_factory=dict)
    api_warnings: list[str] = field(default_factory=list)


def score_cell(cell: Cell, reference: str | None, settings: Settings | None = None, dictionary=None) -> Cell:
    if cell.error:
        return cell
    from .page_score import score_text
    settings = settings or Settings()
    cell.quality = score_text(cell.text, "ocr", settings, dictionary).score
    if dictionary is not None:
        unk, n, _ = dictionary.unknown_ratio(cell.text)
        cell.unknown = round(unk, 3) if n else None
    if reference:
        cell.accuracy = match_score(reference, cell.text)
    return cell


def _avg(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


@dataclass
class RankRow:
    model_id: str
    label: str
    dpi: int
    accuracy: float | None
    quality: float | None
    unknown: float | None
    seconds: float | None
    load_seconds: float | None
    errors: int


def ranking(report: Report) -> list[RankRow]:
    """ترتيب (نموذج، دقة): بالتطابق مع المرجع إن وُجد، ثم درجة الجودة، ثم السرعة."""
    rows = []
    for m in report.models:
        for d in report.dpis:
            cells = [c for (mid, dpi, _), c in report.results.items() if mid == m.id and dpi == d]
            if not cells:
                continue
            rows.append(RankRow(
                m.id, m.label, d,
                _avg(c.accuracy for c in cells), _avg(c.quality for c in cells), _avg(c.unknown for c in cells),
                _avg(c.seconds for c in cells if not c.error), report.load_seconds.get(m.id),
                sum(1 for c in cells if c.error)))
    rows.sort(key=lambda r: (-(r.accuracy if r.accuracy is not None else -1), -(r.quality or 0), r.errors,
                             r.seconds if r.seconds is not None else 1e9))
    return rows


# ------------------------------------------------------------------ التشغيل
def run(pdf: Path, pages: list[int], models: list[cat.ModelSpec], report: Report, provider_factory=None,
        log=print, stop: threading.Event | None = None, dpis: tuple[int, ...] = DPIS,
        on_cell: Callable[[str, int, int, Cell], None] | None = None, dictionary=None) -> None:
    from .providers.local_llama import start_local_provider

    stop = stop or threading.Event()
    report.dpis = tuple(dpis)
    exe, fallback = mm.engine_for(Settings.load().use_gpu)
    with fitz.open(pdf) as doc:
        renders = {(dpi, p): render_page_png(doc[p - 1], dpi) for dpi in dpis for p in pages}
    for p in pages:
        report.images[p] = renders[(min(dpis), p)]
    settings = Settings.load()
    for m in models:
        if stop.is_set():
            raise Stopped()
        log(f"— {m.label}: تشغيل المحرّك…")
        t0 = time.monotonic()
        try:
            if provider_factory:
                provider = provider_factory(m)
            else:
                model_p, mmproj_p = mm.model_paths(m)
                provider = start_local_provider(exe, model_p, mmproj_p, app_data_dir() / "logs", m.id, stop=stop,
                                              prompt=m.prompt, gpu=bool(fallback), fallback_exe=fallback)
        except Exception as e:
            if stop.is_set():
                raise Stopped() from e
            report.excluded[m.label] = f"تعذّر تشغيل المحرّك: {e}"
            log(f"  ✗ {e}")
            continue
        report.load_seconds[m.id] = round(time.monotonic() - t0, 1)
        report.models.append(m)
        try:
            for dpi in dpis:
                for p in pages:
                    if stop.is_set():
                        raise Stopped()
                    t1 = time.monotonic()
                    try:
                        r = provider.ocr(OcrRequest([PageImage(p, renders[(dpi, p)], dpi)]))[0]
                        cell = Cell(r.markdown, round(time.monotonic() - t1, 1))
                    except EngineUnavailable:
                        raise
                    except Exception as e:
                        cell = Cell(error=str(e), seconds=round(time.monotonic() - t1, 1))
                    score_cell(cell, report.reference.get(p) or report.reference.get(0), settings, dictionary)
                    report.results[(m.id, dpi, p)] = cell
                    acc = f" — دقة {cell.accuracy:.0%}" if cell.accuracy is not None else ""
                    log(f"  صفحة {p} @ {dpi}DPI: {cell.seconds}s{acc}{' ✗ ' + cell.error if cell.error else ''}")
                    if on_cell:
                        on_cell(m.id, dpi, p, cell)
        except EngineUnavailable as e:
            log(f"  ✗ توقف المحرّك: {e}")
        finally:
            provider.close()


PADDLE_ID = "paddle"


def paddle_spec() -> cat.ModelSpec:
    """مدخل شكلي يمثّل PaddleOCR في التقرير (ليس نموذج GGUF)."""
    from .models_catalog import FileSpec
    empty = FileSpec("", 0, "")
    return cat.ModelSpec(PADDLE_ID, "PaddleOCR PP-OCRv5 (ONNX)", "", empty, empty)


def run_paddle(pdf: Path, pages: list[int], report: Report, log=print, stop: threading.Event | None = None,
               worker_factory=None, on_cell: Callable[[str, int, int, Cell], None] | None = None,
               dictionary=None) -> None:
    """PaddleOCR (إن كان مثبّتاً) على نفس الصفحات بدقة رسمه الخاصة، بنفس تنسيق بقية المحرّكات."""
    from . import paddle_engine as pe
    stop = stop or threading.Event()
    settings = Settings.load()
    dpi = pe.RENDER_DPI
    log("— PaddleOCR: تشغيل العملية المنفصلة…")
    t0 = time.monotonic()
    try:
        worker = (worker_factory or (lambda: pe.PaddleWorker().start()))()
    except Exception as e:
        report.excluded["PaddleOCR"] = f"تعذّر التشغيل: {e}"
        log(f"  ✗ {e}")
        return
    spec = paddle_spec()
    report.load_seconds[PADDLE_ID] = round(time.monotonic() - t0, 1)
    report.models.append(spec)
    report.dpis = tuple(sorted(set(report.dpis) | {dpi}))
    try:
        with fitz.open(pdf) as doc:
            for p in pages:
                if stop.is_set():
                    raise Stopped()
                png, scale = pe.page_image(doc[p - 1])
                report.images.setdefault(p, render_page_png(doc[p - 1], min(report.dpis)))
                t1 = time.monotonic()
                try:
                    res = pe.ocr_page(worker, png, settings.paddle_min_conf, mark_low=True, scale=scale)
                    cell = Cell(res.markdown, round(time.monotonic() - t1, 1), confidence=round(res.min_conf, 3),
                                low_lines=len(res.low_lines))
                except Exception as e:
                    cell = Cell(error=str(e), seconds=round(time.monotonic() - t1, 1))
                score_cell(cell, report.reference.get(p) or report.reference.get(0), settings, dictionary)
                report.results[(PADDLE_ID, dpi, p)] = cell
                acc = f" — دقة {cell.accuracy:.0%}" if cell.accuracy is not None else ""
                conf = f" — ثقة ≥ {cell.confidence:.2f}، أسطر ضعيفة {cell.low_lines}" if cell.confidence is not None else ""
                log(f"  صفحة {p} @ {dpi}DPI: {cell.seconds}s{acc}{conf}{' ✗ ' + cell.error if cell.error else ''}")
                if on_cell:
                    on_cell(PADDLE_ID, dpi, p, cell)
    finally:
        worker.close()


def run_api(pdf: Path, pages: list[int], report: Report, slots, gate, settings, log=print) -> None:
    """كل مزوّد مفعّل على نفس الصفحات، بنفس التقسيم الذي يستخدمه البرنامج. يمر عبر cloud_gate."""
    from .providers.batching import build_prompt, chunk_pages, split_and_check
    from .providers.render import render_page_jpeg

    ticket = gate.open_file(pdf)  # يرفض في الوضع الداخلي
    with fitz.open(pdf) as doc:
        images = {p: render_page_jpeg(doc[p - 1], p, settings.api_dpi, settings.api_max_dim,
                                      settings.api_jpeg_quality) for p in pages}
    for slot in slots:
        pid = slot.id
        size = slot.batch_size(settings)
        report.api_names[pid] = slot.spec.name
        report.api_batch[pid] = size
        model = slot.state.model or slot.spec.default_model
        log(f"— {slot.spec.name} ({model}), دفعة {size}")
        for chunk in chunk_pages(sorted(pages), size):
            imgs = [images[p] for p in chunk]
            t0 = time.monotonic()
            try:
                text = slot.limiter.call(lambda: gate.send(ticket, slot.provider, build_prompt(chunk),
                                                           OcrRequest(imgs), model))
                per = round((time.monotonic() - t0) / len(chunk), 1)
                chk = split_and_check(text, imgs)
                for p in chunk:
                    if p not in chk.pages:
                        report.api_warnings.append(f"{slot.spec.name}: علامة الصفحة {p} غائبة من الناتج")
                    cell = Cell(chk.pages.get(p, ""), per, "" if p in chk.pages else "علامة الصفحة غائبة")
                    report.api_results[(pid, p)] = score_cell(cell, report.reference.get(p), settings)
                for prob in chk.problems:
                    if "مفقودة" not in prob:
                        report.api_warnings.append(f"{slot.spec.name} ص{chunk}: {prob}")
                log(f"  صفحات {chunk}: {per}s/صفحة{'' if chk.ok else ' ⚠ ' + '؛ '.join(chk.problems)}")
            except Exception as e:
                for p in chunk:
                    report.api_results[(pid, p)] = Cell(error=f"{type(e).__name__}: {e}",
                                                        seconds=round(time.monotonic() - t0, 1))
                log(f"  ✗ {chunk}: {e}")


# ------------------------------------------------------------------ السجل
def history_path() -> Path:
    d = app_data_dir() / "lab"
    d.mkdir(parents=True, exist_ok=True)
    return d / "results.jsonl"


def save_history(report: Report, path: Path | None = None) -> int:
    """يضيف صفاً لكل (نموذج، دقة) — بلا نص المستند، فقط الأرقام."""
    rows = ranking(report)
    if not rows:
        return 0
    stamp = datetime.now().isoformat(timespec="seconds")
    with open(path or history_path(), "a", encoding="utf-8") as f:
        for r in rows:
            d = asdict(r)
            d.update(time=stamp, pdf=report.pdf_name, pages=report.pages, reference=report.reference_source)
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return len(rows)


def load_history(path: Path | None = None) -> list[dict]:
    p = path or history_path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def history_summary(rows: list[dict]) -> list[dict]:
    """متوسط كل (نموذج، دقة) عبر كل التشغيلات المحفوظة، مرتّباً بنفس معيار ranking."""
    groups: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        groups.setdefault((r.get("label", r.get("model_id", "?")), r.get("dpi", 0)), []).append(r)
    out = []
    for (label, dpi), rs in groups.items():
        out.append({"label": label, "dpi": dpi, "runs": len(rs),
                    "accuracy": _avg(r.get("accuracy") for r in rs), "quality": _avg(r.get("quality") for r in rs),
                    "seconds": _avg(r.get("seconds") for r in rs)})
    out.sort(key=lambda r: (-(r["accuracy"] if r["accuracy"] is not None else -1), -(r["quality"] or 0),
                            r["seconds"] if r["seconds"] is not None else 1e9))
    return out


# ------------------------------------------------------------------ HTML
CSS = """
body{font-family:'Segoe UI',Tahoma,sans-serif;margin:16px;background:#fafafa;color:#222}
h1{font-size:20px} h2{font-size:17px;margin-top:28px;border-bottom:2px solid #ccc}
.row{display:flex;gap:12px;align-items:flex-start;overflow-x:auto}
.img{flex:0 0 420px;position:sticky;top:0} .img img{width:420px;border:1px solid #bbb;background:#fff}
.res{flex:0 0 420px;background:#fff;border:1px solid #ddd;padding:8px}
.res h3{font-size:13px;margin:0 0 6px;direction:ltr;text-align:left}
.res pre{white-space:pre-wrap;word-wrap:break-word;font-family:inherit;font-size:13px;direction:rtl;text-align:right;margin:0}
.err{color:#a00} mark.low{background:#ffe08a} .t{color:#555;font-weight:normal} table{border-collapse:collapse} td,th{border:1px solid #ccc;padding:4px 8px}
.best{background:#e8f4ea}
"""


def _pct(v) -> str:
    return "—" if v is None else f"{v:.0%}"


def _metrics(c: Cell) -> str:
    parts = [f"{c.seconds}s"]
    if c.accuracy is not None:
        parts.append(f"دقة {_pct(c.accuracy)}")
    if c.quality is not None:
        parts.append(f"جودة {_pct(c.quality)}")
    if c.confidence is not None:
        parts.append(f"ثقة ≥ {c.confidence:.2f}")
    if c.low_lines:
        parts.append(f"أسطر ضعيفة {c.low_lines}")
    return " · ".join(parts)


def _text_html(text: str) -> str:
    """النص مع تمييز الأسطر منخفضة الثقة (تبدأ بـ [؟])."""
    from .paddle_engine import LOW_MARK
    return "\n".join(f"<mark class='low'>{html.escape(ln)}</mark>" if ln.startswith(LOW_MARK) else html.escape(ln)
                     for ln in text.split("\n"))


def render_html(report: Report) -> str:
    e = html.escape
    out = [f"<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'><title>مختبر النماذج — {e(report.pdf_name)}</title>"
           f"<style>{CSS}</style></head><body>",
           f"<h1>مقارنة النماذج — {e(report.pdf_name)}</h1>",
           "<p>⚠ هذا التقرير يحتوي صور الصفحات ونصها. لا تشاركه.</p>"]
    if report.reference_source:
        out.append(f"<p>النص المرجعي: {e(report.reference_source)}</p>")
    rows = ranking(report)
    if rows:
        out.append("<h2>الترتيب</h2><table><tr><th>#</th><th>النموذج</th><th>DPI</th><th>الدقة</th>"
                   "<th>الجودة</th><th>كلمات مجهولة</th><th>ث/صفحة</th><th>التحميل (ث)</th><th>أخطاء</th></tr>")
        for i, r in enumerate(rows, 1):
            cls = " class='best'" if i == 1 else ""
            out.append(f"<tr{cls}><td>{i}</td><td dir='ltr'>{e(r.label)}</td><td>{r.dpi}</td><td>{_pct(r.accuracy)}</td>"
                       f"<td>{_pct(r.quality)}</td><td>{_pct(r.unknown)}</td><td>{r.seconds if r.seconds is not None else '—'}</td>"
                       f"<td>{r.load_seconds if r.load_seconds is not None else '—'}</td><td>{r.errors}</td></tr>")
        out.append("</table>")
    if report.api_names:
        out.append("<h2>مزوّدات الـ API</h2><table><tr><th>المزوّد</th><th>حجم الدفعة</th><th>متوسط/صفحة (ث)</th></tr>")
        for pid, name in report.api_names.items():
            ts = [c.seconds for (q, _), c in report.api_results.items() if q == pid and c.seconds is not None]
            avg = round(sum(ts) / len(ts), 1) if ts else "—"
            out.append(f"<tr><td dir='ltr'>{e(name)}</td><td>{report.api_batch.get(pid)}</td><td>{avg}</td></tr>")
        out.append("</table>")
        if report.api_warnings:
            out.append("<ul class='err'>" + "".join(f"<li>⚠ {e(w)}</li>" for w in report.api_warnings) + "</ul>")
    for p in report.pages:
        out.append(f"<h2>صفحة {p}</h2><div class='row'>")
        out.append(f"<div class='img'><img src='data:image/png;base64,{base64.b64encode(report.images[p]).decode()}'></div>")
        ref = report.reference.get(p)
        if ref:
            out.append(f"<div class='res'><h3>النص المرجعي</h3><pre>{e(ref)}</pre></div>")
        for m in report.models:
            for d in report.dpis:
                if (m.id, d, p) not in report.results and (m.id == PADDLE_ID or d not in DPIS):
                    continue  # PaddleOCR يعمل بدقته الخاصة فقط، والنماذج الأخرى بدقاتها
                c = report.results.get((m.id, d, p), Cell(error="لم تُشغّل"))
                body = f"<pre class='err'>{e(c.error)}</pre>" if c.error else f"<pre>{_text_html(c.text)}</pre>"
                out.append(f"<div class='res'><h3>{e(m.label)} — {d} DPI <span class='t'>({_metrics(c)})</span></h3>{body}</div>")
        for pid, name in report.api_names.items():
            c = report.api_results.get((pid, p), Cell(error="لم يُشغّل"))
            body = f"<pre class='err'>{e(c.error)}</pre>" if c.error else f"<pre>{e(c.text)}</pre>"
            out.append(f"<div class='res'><h3>API: {e(name)} — دفعة {report.api_batch.get(pid)} "
                       f"<span class='t'>({c.seconds}s/صفحة)</span></h3>{body}</div>")
        out.append("</div>")
    out.append("<h2>النماذج المستبعدة</h2><table><tr><th>النموذج</th><th>السبب</th></tr>")
    for name, reason in report.excluded.items():
        out.append(f"<tr><td dir='ltr'>{e(name)}</td><td>{e(reason)}</td></tr>")
    out.append("</table></body></html>")
    return "\n".join(out)


# ------------------------------------------------------------------ سطر الأوامر
def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="مقارنة النماذج المحلية على صفحات مختارة")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--pages", required=True, help="مثال: 2,5,9 أو 3-5 (حتى 5 صفحات)")
    ap.add_argument("--models", default="", help="معرّفات مفصولة بفاصلة (الافتراضي: كل المثبّت)")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--reference", type=Path, default=None, help="ملف نص مرجعي (=== صفحة N === بين الصفحات)")
    ap.add_argument("--api", action="store_true", help="أضف مزوّدات الـ API المفعّلة (يرسل الصفحات للخارج)")
    ap.add_argument("--no-paddle", action="store_true", help="لا تشغّل PaddleOCR حتى لو كان مثبّتاً")
    args = ap.parse_args(argv)

    pdf = args.pdf.resolve()
    if not pdf.is_file():
        print(f"الملف غير موجود: {pdf}", file=sys.stderr)
        return 2
    with fitz.open(pdf) as doc:
        try:
            pages = parse_pages(args.pages, doc.page_count)
        except ValueError as ex:
            print(ex, file=sys.stderr)
            return 2

    requested = [x.strip() for x in args.models.split(",") if x.strip()] or None
    models, excluded = select_models(requested)
    if models and mm.llama_exe() is None:
        for m in models:
            excluded[m.label] = "llama-server.exe غير مثبّت (نزّله أو انسخه من البرنامج)"
        models = []

    report = Report(pdf.name, pages, excluded=excluded)
    if args.reference:
        report.reference, report.reference_source = parse_reference_file(args.reference), args.reference.name
    else:
        report.reference = text_layer_reference(pdf, pages)
        report.reference_source = "طبقة النص في الملف" if report.reference else ""
    slots = []
    if args.api:
        from .audit_log import AuditLog
        from .cloud_gate import CLOUD, CloudGate
        from .dispatcher import build_slots
        from .providers.catalog import ProviderConfig
        from .rate_limit import UsageCounter
        from .secrets_store import SecretsStore
        data = app_data_dir()
        gate = CloudGate(AuditLog(data / "sent_log.csv"), mode=CLOUD)
        settings = Settings.load()
        slots = build_slots(ProviderConfig(), SecretsStore(), UsageCounter(data / "usage.json"), threading.Event(),
                            emit=lambda kind, **kw: print(kw.get("text", "")))
        slots = [s for s in slots if not s.down and not s.is_ocr]
        if not slots:
            print("لا يوجد مزوّد API مفعّل بمفتاح (أو كلها منتهية الحصة اليوم).")
    from . import paddle_engine as pe
    use_paddle = not args.no_paddle and (requested is None or PADDLE_ID in requested) and pe.is_available()
    if not models and not slots and not use_paddle:
        print("لا يوجد نموذج قابل للتشغيل. الأسباب:")
        for k, v in excluded.items():
            print(f"  - {k}: {v}")
    from .dictionary import ArabicDictionary
    dictionary = ArabicDictionary()
    run(pdf, pages, models, report, dictionary=dictionary)
    if use_paddle:
        run_paddle(pdf, pages, report, dictionary=dictionary)
    if slots:
        try:
            run_api(pdf, pages, report, slots, gate, settings)
        finally:
            for s in slots:
                s.provider.close()
        for w in report.api_warnings:
            print(f"⚠ {w}")
    save_history(report)
    out = args.out or pdf.with_name(pdf.stem + ".benchmark.html")
    out.write_text(render_html(report), encoding="utf-8")
    print(f"التقرير: {out}")
    return 0
