"""درجة جودة لكل صفحة مكتملة، من مؤشرات قابلة للضبط (الإعدادات):

  (أ) brackets       نسبة علامات [ ... ] (مثل [غير واضح] أو [؟]) مع تجاهل أنماط Markdown:
                     الروابط [نص](رابط)، مربعات [x] و[ ]، الحواشي [^1]، وتعريفات المراجع [1]: …
  (ب) unknown_words  نسبة الكلمات العربية غير الموجودة في القاموس المحلي (dictionary.py).
                     لا يُطبّق على صفحات طبقة النص (القاموس مبني منها) ولا قبل أن يبلغ القاموس
                     dict_min_words كلمة.
  (ج) repetition     تكرار حروف/كلمات غير طبيعي (علامة تعلّق النموذج) — quality.repetition_problem.
  (د) too_short      ناتج قصير جداً مقارنة بكثافة حبر صورة الصفحة — لصفحات OCR/API فقط.

الدرجة = 1 − مجموع (وزن المؤشر × min(1، القيمة/الحد)). الصفحة ضعيفة إذا تجاوز أي مؤشر حدّه
أو نزلت الدرجة عن weak_score_threshold. النتيجة تُحفظ في checkpoint تحت "score".
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from . import checkpoint as cp
from .config import Settings
from .dictionary import ArabicDictionary
from .quality import repetition_problem, suspiciously_short, visible_chars

SCORE_VERSION = 1

WEIGHTS = {"brackets": 0.4, "unknown_words": 0.4, "repetition": 1.0, "too_short": 0.5}

_BRACKET = re.compile(r"\[([^\[\]\n]{0,60})\]")
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)


def bracket_marks(text: str) -> int:
    body = re.sub(r"<!--.*?-->", " ", text or "", flags=re.S)
    n = 0
    for m in _BRACKET.finditer(body):
        inner = m.group(1)
        after = body[m.end():m.end() + 1]
        line_start = body.rfind("\n", 0, m.start()) + 1
        if after == "(":                                  # رابط [نص](رابط)
            continue
        if after == ":" and not body[line_start:m.start()].strip():   # تعريف مرجع
            continue
        if inner in ("x", "X", " ", "") and body[line_start:m.start()].strip() in ("", "-", "*", "+"):
            continue                                       # مربع اختيار
        if inner.startswith("^"):                          # حاشية
            continue
        if m.start() > 0 and body[m.start() - 1] == "!":   # صورة ![..](..)
            continue
        n += 1
    return n


@dataclass
class PageScore:
    score: float
    weak: bool
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def to_dict(self, sig: str) -> dict:
        return {"v": SCORE_VERSION, "sig": sig, "score": self.score, "weak": self.weak,
                "reasons": self.reasons, "details": self.details}


def score_text(text: str, method: str, settings: Settings, dictionary: ArabicDictionary | None = None,
               ink: float | None = None) -> PageScore:
    is_text_layer = method == "text_layer"
    words = len(_WORDS.findall(re.sub(r"<!--.*?-->", " ", text or "", flags=re.S)))
    values: dict[str, float] = {}
    hits: list[str] = []
    details: dict = {"words": words}

    # (أ) علامات [ ]
    marks = bracket_marks(text)
    ratio = marks / max(words, 1)
    details["brackets"] = marks
    if marks >= settings.bracket_min_count:
        values["brackets"] = ratio / settings.bracket_max_ratio if settings.bracket_max_ratio else 0.0
    if marks >= settings.bracket_min_count and ratio >= settings.bracket_max_ratio:
        hits.append("brackets")

    # (ب) القاموس
    if (not is_text_layer and dictionary is not None and len(dictionary) >= settings.dict_min_words):
        unk, n_ar, sample = dictionary.unknown_ratio(text)
        details["unknown_ratio"] = round(unk, 3)
        if sample:
            details["unknown_sample"] = sample
        if n_ar >= 20:
            values["unknown_words"] = unk / settings.unknown_max_ratio if settings.unknown_max_ratio else 0.0
            if unk >= settings.unknown_max_ratio:
                hits.append("unknown_words")
    elif not is_text_layer:
        details["unknown_ratio"] = None  # القاموس ما زال صغيراً

    # (ج) التكرار
    rep = repetition_problem(text)
    if rep:
        details["repetition"] = rep
        values["repetition"] = 1.0
        hits.append("repetition")

    # (د) قصير مقارنة بالحبر
    if not is_text_layer and ink is not None:
        details["ink"] = round(ink, 4)
        details["chars"] = visible_chars(text)
        if suspiciously_short(text, ink, settings.short_fraction):
            values["too_short"] = 1.0
            hits.append("too_short")

    score = 1.0 - sum(WEIGHTS[k] * min(1.0, v) for k, v in values.items())
    score = round(max(0.0, score), 3)
    reasons = list(hits)
    if not reasons and score < settings.weak_score_threshold:
        reasons.append("low_score")
    return PageScore(score, bool(reasons), reasons, details)


def _sig(text: str, settings: Settings, dictionary: ArabicDictionary | None) -> str:
    knobs = (settings.weak_score_threshold, settings.bracket_max_ratio, settings.bracket_min_count,
             settings.unknown_max_ratio, settings.dict_min_words, settings.short_fraction)
    dict_bucket = len(dictionary) // 1000 if dictionary is not None else -1
    raw = f"{SCORE_VERSION}|{knobs}|{dict_bucket}|".encode() + (text or "").encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def score_state(state: cp.FileState, settings: Settings, dictionary: ArabicDictionary | None,
                ink_of=None) -> dict[int, PageScore]:
    """يحسب درجة كل صفحة مكتملة لم تُحسب درجتها بنفس النص ونفس الإعدادات. ink_of(n) اختياري.
    يعيد الدرجات المحسوبة حديثاً فقط؛ وكل الدرجات محفوظة في state تحت "score"."""
    fresh: dict[int, PageScore] = {}
    changed = False
    for n in range(1, state.page_count + 1):
        p = state.page(n)
        if not p or p.get("status") != cp.DONE:
            if p and "score" in p:
                p.pop("score")
                changed = True
            continue
        text = state.read_fragment(n)
        sig = _sig(text, settings, dictionary)
        if (p.get("score") or {}).get("sig") == sig:
            continue
        method = p.get("method", "")
        ink = ink_of(n) if ink_of and method != "text_layer" else None
        ps = score_text(text, method, settings, dictionary, ink)
        p["score"] = ps.to_dict(sig)
        fresh[n] = ps
        changed = True
    if changed:
        state.flush()
    return fresh


def weak_pages(state: cp.FileState) -> dict[int, list[str]]:
    out = {}
    for n in range(1, state.page_count + 1):
        sc = (state.page(n) or {}).get("score") or {}
        if sc.get("weak"):
            out[n] = sc.get("reasons", [])
    return out
