"""مقارنة سلسلة OCR على نفس الصفحات (يشغّلها benchmark.py --ocr-chain):

  (1) OCR.space خام  (2) OCR.space + المصحّح  (3) نموذج الرؤية مباشرة

لكل صفحة: الزمن، وعدد التعديلات المطبّقة والمرفوضة، وتعليم أي صفحة اختلف فيها رقم أو اسم بين
الخام والمصحّح. كل إرسال عبر cloud_gate (يرفض في الوضع الداخلي).
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz

from . import corrector
from .facts import find_names, normalize_for_compare
from .providers.base import OcrRequest
from .providers.batching import build_prompt, split_and_check


@dataclass
class Row:
    page: int
    mode: str
    seconds: float
    chars: int = 0
    applied: int = 0
    rejected: int = 0
    flag: str = ""
    error: str = ""


def fact_diff(raw: str, fixed: str) -> str:
    """"" إن تطابقت الأرقام والأسماء بين الخام والمصحّح، وإلا وصف الفرق."""
    nums = lambda t: sorted(re.findall(r"\d+", normalize_for_compare(t)))  # noqa: E731
    names = lambda t: sorted(n for _r, n, _x in find_names(t))            # noqa: E731
    out = []
    if nums(raw) != nums(fixed):
        out.append("رقم مختلف")
    if names(raw) != names(fixed):
        out.append("اسم مختلف")
    return "، ".join(out)


def compare(pdf: Path, pages: list[int], gate, settings, ocr_slot, llm_slot, log=print) -> list[Row]:
    from .providers.render import render_page_jpeg
    ticket = gate.open_file(pdf)
    rows: list[Row] = []
    with fitz.open(pdf) as doc:
        images = {p: render_page_jpeg(doc[p - 1], p, settings.api_dpi, settings.api_max_dim,
                                      settings.api_jpeg_quality) for p in pages}
    for p in pages:
        img = images[p]
        raw = ""
        if ocr_slot is not None:
            t0 = time.monotonic()
            try:
                text = ocr_slot.limiter.call(lambda: gate.send(ticket, ocr_slot.provider, "", OcrRequest([img]),
                                                               ocr_slot.provider.model))
                raw = f"<!-- page:{p} -->\n" + split_and_check(text, [img]).pages.get(p, "")
                rows.append(Row(p, "ocr_raw", round(time.monotonic() - t0, 1), len(raw)))
            except Exception as e:
                rows.append(Row(p, "ocr_raw", round(time.monotonic() - t0, 1), error=f"{type(e).__name__}: {e}"))
        if raw and llm_slot is not None:
            t0 = time.monotonic()
            try:
                res = corrector.correct(raw, lambda t: llm_slot.limiter.call(lambda: gate.send_text(
                    ticket, llm_slot.provider, corrector.TEXT_PROMPT, t, llm_slot.provider.model, purpose="correct")))
                flag = fact_diff(raw, res.text) or ("" if res.ok else f"تُرك خاماً: {res.problem}")
                rows.append(Row(p, "ocr_corrected", round(time.monotonic() - t0, 1), len(res.text),
                                res.applied, res.rejected, flag))
            except Exception as e:
                rows.append(Row(p, "ocr_corrected", round(time.monotonic() - t0, 1), error=f"{type(e).__name__}: {e}"))
        if llm_slot is not None:
            t0 = time.monotonic()
            try:
                text = llm_slot.limiter.call(lambda: gate.send(ticket, llm_slot.provider, build_prompt([p]),
                                                               OcrRequest([img]), llm_slot.provider.model))
                body = split_and_check(text, [img]).pages.get(p, "")
                rows.append(Row(p, "vision", round(time.monotonic() - t0, 1), len(body)))
            except Exception as e:
                rows.append(Row(p, "vision", round(time.monotonic() - t0, 1), error=f"{type(e).__name__}: {e}"))
        for r in rows:
            if r.page == p:
                log(f"ص{p} {r.mode}: {r.seconds}s {r.error or ''}{(' ⚠ ' + r.flag) if r.flag else ''}")
    return rows


def render(pdf_name: str, rows: list[Row]) -> str:
    names = {"ocr_raw": "OCR.space خام", "ocr_corrected": "OCR.space + المصحّح", "vision": "نموذج الرؤية"}
    out = [f"# مقارنة سلسلة OCR — {pdf_name}", "",
           "| الصفحة | الطريقة | الزمن (ث) | الحروف | مطبّق | مرفوض | تنبيه |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        out.append(f"| {r.page} | {names[r.mode]} | {r.seconds} | {r.chars} | {r.applied} | {r.rejected} | "
                   f"{(r.error or r.flag).replace('|', '/')} |")
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    from .audit_log import AuditLog
    from .cloud_gate import CLOUD, CloudGate
    from .config import Settings, app_data_dir
    from .dispatcher import build_slots
    from .model_lab import parse_pages
    from .providers.catalog import ProviderConfig
    from .rate_limit import UsageCounter
    from .secrets_store import SecretsStore
    ap = argparse.ArgumentParser(description="مقارنة OCR.space خام / مع المصحّح / نموذج الرؤية")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--pages", required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    pdf = args.pdf.resolve()
    with fitz.open(pdf) as doc:
        pages = parse_pages(args.pages, doc.page_count)
    data = app_data_dir()
    gate = CloudGate(AuditLog(data / "sent_log.csv"), mode=CLOUD)
    slots = [s for s in build_slots(ProviderConfig(), SecretsStore(), UsageCounter(data / "usage.json"),
                                    threading.Event(), emit=lambda kind, **kw: print(kw.get("text", "")))
             if not s.down]
    ocr = next((s for s in slots if s.is_ocr), None)
    llm = next((s for s in slots if not s.is_ocr), None)
    if ocr is None and llm is None:
        print("لا يوجد مزوّد مفعّل بمفتاح.", file=sys.stderr)
        return 2
    try:
        rows = compare(pdf, pages, gate, Settings.load(), ocr, llm)
    finally:
        for s in slots:
            s.provider.close()
    out = args.out or pdf.with_name(pdf.stem + ".ocr-chain.md")
    out.write_text(render(pdf.name, rows), encoding="utf-8")
    print(f"التقرير: {out}")
    return 0
