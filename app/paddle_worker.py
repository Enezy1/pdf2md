"""عملية PaddleOCR المنفصلة (worker): نماذج PP-OCRv5 بصيغة ONNX عبر ONNX Runtime على المعالج.

لا يُستورد من البرنامج. يشغّله app/paddle_engine.py هكذا:
    python -I -S paddle_worker.py --root %APPDATA%\\pdf2md\\engines\\paddle
-S: لا تُحمَّل مكتبات بايثون العامة؛ المكتبات من <root>\\lib فقط (معزولة عن البرنامج).

البروتوكول: سطر JSON لكل طلب على stdin وسطر JSON لكل رد على stdout.
    {"id": 1, "op": "ocr", "png": "<base64>", "scale": 1} → {"id": 1, "ok": true, "lines": [{"text","conf","box"}], "ms": 812}
    {"id": 2, "op": "quit"}
أول سطر يُكتب عند الجاهزية: {"ready": true, ...} أو {"ready": false, "error": "..."}.

الشبكة ممنوعة: كل محاولة اتصال ترفع NetworkBlocked قبل استيراد أي مكتبة.
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import sys
import time


class NetworkBlocked(OSError):
    pass


def block_network() -> None:
    def deny(*_a, **_k):
        raise NetworkBlocked("PaddleOCR worker: الاتصال بالشبكة ممنوع")

    class _NoSocket(socket.socket):
        def connect(self, *_a, **_k):
            deny()

        def connect_ex(self, *_a, **_k):
            deny()

        def sendto(self, *_a, **_k):
            deny()

    socket.socket = _NoSocket
    socket.create_connection = deny
    socket.getaddrinfo = deny
    socket.gethostbyname = deny


DET_MAX_SIDE = 3600
DET_THRESH, BOX_THRESH, UNCLIP, MIN_SIZE = 0.3, 0.5, 1.6, 3
REC_H = 48
GAP_RATIO = 0.3
SKEW_SIDE, SKEW_MAX, SKEW_MIN = 1200, 8.0, 0.3   # تقدير الميل على صورة مصغّرة؛ أقل من 0.3° لا يُقوَّم
_LATIN_RUN = re.compile(r"[a-zA-Z0-9٠-٩۰-۹ :*./%+\-,،٫٬]")
_INDIC = "٠-٩۰-۹"
_DIGIT = re.compile(f"[0-9{_INDIC}]")
_NUMERIC = re.compile(f"^[0-9{_INDIC}/\\\\\\-–.,،:()]*[0-9{_INDIC}][0-9{_INDIC}/\\\\\\-–.,،:()]*$")
STRIP_H = 56               # ارتفاع شريط الأرقام المرسل (بكسل): يكفي لقراءة الأرقام وأصغر بكثير من الصفحة
SLASH_TALL = 1.35         # مكوّن أطول من الأرقام بـ 35% في مقطع أرقام = "/"
ZERO_MAX = 0.45           # مكوّن أصغر من 45% من ارتفاع الأرقام = نقطة الصفر الهندي
_DOT_AFTER =re.compile(f"([{_INDIC}]) ?[.·•]")
_DOT_BEFORE = re.compile(f"[.·•] ?([{_INDIC}])")


def fix_indic_zeros(raw: str) -> str:
    """الصفر الهندي "٠" نقطة في الطباعة، فيقرؤه النموذج "." أو "·" أو "•" (قيس على مستند حقيقي:
    ٤٣٠٠٠ ← "٤٣ . ·"). النقطة الملاصقة لرقم هندي تُعاد صفراً، وتُحذف المسافة بينهما.
    لا يمس الأرقام الإنجليزية (12.5 عشري حقيقي)."""
    prev = None
    while prev != raw:
        prev = raw
        raw = _DOT_AFTER.sub(lambda m: m.group(1) + "٠", raw)
        raw = _DOT_BEFORE.sub(lambda m: "٠" + m.group(1), raw)
    return raw


def pred_reverse(pred: str) -> str:
    """نموذج العربية يخرج النص بترتيب الصورة (يسار←يمين): نعكسه مع إبقاء الأرقام واللاتيني كما هي
    (نفس منطق PaddleOCR BaseRecLabelDecode.pred_reverse)."""
    parts, run = [], ""
    for c in pred:
        if _LATIN_RUN.match(c):
            run += c
        else:
            if run:
                parts.append(run)
            parts.append(c)
            run = ""
    if run:
        parts.append(run)
    # المسافات على طرفي مقطع الأرقام/اللاتيني تبقى فواصل مستقلة حتى لا تلتصق بالكلمة المجاورة بعد العكس
    out = []
    for p in parts:
        core = p.strip(" ")
        if core and core != p:
            lead, trail = p[:len(p) - len(p.lstrip(" "))], p[len(p.rstrip(" ")):]
            out += [lead, core, trail]
        else:
            out.append(p)
    return "".join(reversed([x for x in out if x]))


class Engine:
    def __init__(self, root: str):
        import cv2  # noqa: F401
        import numpy as np  # noqa: F401
        import onnxruntime as ort
        self.cv2, self.np = cv2, np
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        opts.enable_cpu_mem_arena = False
        cpu = ["CPUExecutionProvider"]
        models = os.path.join(root, "models")
        self.det = ort.InferenceSession(os.path.join(models, "ch_PP-OCRv5_det_mobile.onnx"), opts, providers=cpu)
        self.rec = ort.InferenceSession(os.path.join(models, "arabic_PP-OCRv5_rec_mobile.onnx"), opts, providers=cpu)
        chars = self.rec.get_modelmeta().custom_metadata_map["character"].splitlines()
        self.chars = ["<blank>"] + chars + [" "]

    # ---------------- تقويم الميل
    def skew_angle(self, img) -> float:
        """زاوية ميل الأسطر بالدرجات (موجبة = عكس عقارب الساعة). المسح المائل 2-5° (قيس على أحكام حقيقية)
        يجعل مربعات الأسطر الطويلة تتداخل رأسياً فتندمج أسطر الفقرة كلها في سطر واحد مبعثر.
        الطريقة: الزاوية التي تجعل مجموع الحبر في الصفوف أشد تذبذباً (الأسطر أوضح انفصالاً)."""
        np, cv2 = self.np, self.cv2
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        f = min(1.0, SKEW_SIDE / max(gray.shape))
        small = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        _t, ink = cv2.threshold(small, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        if ink.mean() < 0.005:
            return 0.0
        ink = ink.astype("float32")
        h, w = ink.shape
        c = (w / 2, h / 2)

        def score(a):
            m = cv2.getRotationMatrix2D(c, a, 1.0)
            r = cv2.warpAffine(ink, m, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
            return float(np.square(np.diff(r.sum(1))).sum())

        best = max(np.arange(-SKEW_MAX, SKEW_MAX + 0.01, 0.5), key=score)
        best = max(np.arange(best - 0.5, best + 0.51, 0.1), key=score)
        return float(best) if abs(best) >= SKEW_MIN else 0.0

    def deskew(self, img):
        """(الصورة بعد التقويم، الزاوية). الحواف الجديدة بيضاء."""
        a = self.skew_angle(img)
        if not a:
            return img, 0.0
        cv2 = self.cv2
        h, w = img.shape[:2]
        m = cv2.getRotationMatrix2D((w / 2, h / 2), a, 1.0)
        out = cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT,
                             borderValue=(255, 255, 255))
        return out, a

    # ---------------- الكشف (DB)
    def detect(self, img):
        np, cv2 = self.np, self.cv2
        h, w = img.shape[:2]
        r = min(1.0, DET_MAX_SIDE / max(h, w))
        nh, nw = max(32, int(round(h * r / 32)) * 32), max(32, int(round(w * r / 32)) * 32)
        x = cv2.resize(img, (nw, nh)).astype("float32")
        x = (x / 255.0 - 0.5) / 0.5
        x = x.transpose(2, 0, 1)[None]
        prob = self.det.run(None, {self.det.get_inputs()[0].name: x})[0][0, 0]
        mask = (prob > DET_THRESH).astype("uint8")
        mask = cv2.dilate(mask, np.ones((2, 2), "uint8"))
        contours, _ = cv2.findContours(mask * 255, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        sx, sy = w / nw, h / nh
        boxes = []
        for c in contours[:1000]:
            rect = cv2.minAreaRect(c)
            if min(rect[1]) < MIN_SIZE:
                continue
            pts = cv2.boxPoints(rect)
            if self._box_score(prob, pts) < BOX_THRESH:
                continue
            pts = self._unclip(pts)
            if pts is None:
                continue
            rect = cv2.minAreaRect(pts)
            if min(rect[1]) < MIN_SIZE + 2:
                continue
            pts = cv2.boxPoints(rect)
            pts[:, 0] = np.clip(pts[:, 0] * sx, 0, w - 1)
            pts[:, 1] = np.clip(pts[:, 1] * sy, 0, h - 1)
            boxes.append(self._order(pts))
        return boxes

    def _box_score(self, prob, pts) -> float:
        np, cv2 = self.np, self.cv2
        h, w = prob.shape
        x0, x1 = int(np.clip(pts[:, 0].min(), 0, w - 1)), int(np.clip(pts[:, 0].max(), 0, w - 1))
        y0, y1 = int(np.clip(pts[:, 1].min(), 0, h - 1)), int(np.clip(pts[:, 1].max(), 0, h - 1))
        m = np.zeros((y1 - y0 + 1, x1 - x0 + 1), "uint8")
        p = pts.copy()
        p[:, 0] -= x0
        p[:, 1] -= y0
        cv2.fillPoly(m, [p.astype("int32")], 1)
        return float(cv2.mean(prob[y0:y1 + 1, x0:x1 + 1], m)[0])

    def _unclip(self, pts):
        import pyclipper
        np, cv2 = self.np, self.cv2
        area = cv2.contourArea(pts)
        length = cv2.arcLength(pts, True)
        if length == 0:
            return None
        d = area * UNCLIP / length
        off = pyclipper.PyclipperOffset()
        off.AddPath([tuple(map(int, p)) for p in pts], pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
        out = off.Execute(d)
        if not out:
            return None
        return np.array(out[0], "float32").reshape(-1, 1, 2)

    def _order(self, pts):
        np = self.np
        s = pts.sum(1)
        d = np.diff(pts, axis=1).ravel()
        return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], "float32")

    # ---------------- التعرّف (CTC)
    def crop(self, img, pts):
        np, cv2 = self.np, self.cv2
        w = int(max(np.linalg.norm(pts[0] - pts[1]), np.linalg.norm(pts[2] - pts[3])))
        h = int(max(np.linalg.norm(pts[0] - pts[3]), np.linalg.norm(pts[1] - pts[2])))
        dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], "float32")
        out = cv2.warpPerspective(img, cv2.getPerspectiveTransform(pts, dst), (w, h),
                                  borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC)
        if h and h / max(w, 1) >= 1.5:
            out = np.rot90(out)
        return out

    def _run_rec(self, crops):
        """يعيد لكل صورة قائمة (x داخل الصورة، الحرف، الثقة) بترتيب الصورة (يسار←يمين)."""
        np, cv2 = self.np, self.cv2
        order = sorted(range(len(crops)), key=lambda i: crops[i].shape[1] / max(crops[i].shape[0], 1))
        out = [None] * len(crops)
        for b in range(0, len(order), 6):
            idx = order[b:b + 6]
            ratio = max(320 / REC_H, *(crops[i].shape[1] / max(crops[i].shape[0], 1) for i in idx))
            W = int(REC_H * ratio)
            batch = np.zeros((len(idx), 3, REC_H, W), "float32")
            widths = []
            for k, i in enumerate(idx):
                c = crops[i]
                rw = max(1, min(W, int(np.ceil(REC_H * c.shape[1] / max(c.shape[0], 1)))))
                x = cv2.resize(c, (rw, REC_H)).astype("float32")
                x = (x / 255.0 - 0.5) / 0.5
                batch[k, :, :, :rw] = x.transpose(2, 0, 1)
                widths.append(rw)
            probs = self.rec.run(None, {self.rec.get_inputs()[0].name: batch})[0]
            for k, i in enumerate(idx):
                step = W / probs.shape[1] * crops[i].shape[1] / widths[k]
                out[i] = self._decode_raw(probs[k], step)
        return out

    def _decode_raw(self, p, step: float):
        ids, conf = p.argmax(1), p.max(1)
        chars, prev = [], -1
        for t, c in enumerate(ids):
            if c != prev and c != 0 and c < len(self.chars):
                chars.append(((t + 0.5) * step, self.chars[c], float(conf[t])))
            prev = c
        return chars

    def _recover(self, crop, chars):
        """النموذج قد يُسقط مقطعاً (أرقاماً غالباً) داخل كلمة/سطر مختلط ويُخرج فراغاً بثقة عالية.
        نبحث عن أعمدة فيها حبر ولم يُقرأ عندها أي حرف، ونقرأ كل مقطع منها وحده ثم ندرجه في مكانه."""
        np, cv2 = self.np, self.cv2
        h, w = crop.shape[:2]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        _t, ink = cv2.threshold(gray, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        cols = ink.sum(0) > 0
        rows = np.flatnonzero(ink.sum(1))
        ink_h = (rows[-1] - rows[0] + 1) if len(rows) else h
        covered = np.zeros(w, bool)
        reach = ink_h * 0.45
        for x, _c, _p in chars:
            covered[max(0, int(x - reach)):min(w, int(x + reach) + 1)] = True
        todo = cols & ~covered
        spans, x = [], 0
        while x < w:
            if todo[x]:
                a = x
                while x < w and (todo[x] or not cols[x]) and not covered[x]:
                    x += 1
                b = x
                while b > a and not cols[b - 1]:
                    b -= 1
                if b - a >= ink_h * 0.6:
                    spans.append((max(0, a - 2), min(w, b + 2)))
            x += 1
        if not spans:
            return chars
        subs = self._run_rec([crop[:, a:b] for a, b in spans])
        extra = []
        for (a, b), sub in zip(spans, subs):
            # الاسترداد للأرقام فقط: في العناوين الممدودة بالتطويل (الوقـــائع) يُعيد قراءة حروف
            # قُرئت فعلاً فتتكرر ("الوقان الوقان ح"، "ط ط رفي")
            if sub and sum(c for _x, _ch, c in sub) / len(sub) >= 0.5 and any(_DIGIT.match(ch) for _x, ch, _c in sub):
                extra += [(a - 0.5, " ", 1.0)] + [(a + x, ch, c) for x, ch, c in sub] + [(b + 0.5, " ", 1.0)]
        return sorted(chars + extra, key=lambda t: t[0])

    def recognize(self, crops):
        """(النص بترتيب القراءة، متوسط الثقة) لكل صورة."""
        out = []
        for crop, chars in zip(crops, self._run_rec(crops)):
            chars = self._recover(crop, chars)
            real = [c for _x, ch, c in chars if ch.strip()]
            text = re.sub(r" {2,}", " ", pred_reverse(fix_indic_zeros("".join(ch for _x, ch, _c in chars)))).strip()
            out.append((text, sum(real) / len(real) if real else 0.0))
        return out

    def split_words(self, crop):
        """يقسم صورة السطر عند الأعمدة الخالية من الحبر الأعرض من GAP_RATIO من ارتفاع الحبر. يعيد الكلمات من اليمين."""
        return [crop[:, a:b] for a, b in self.split_spans(crop)][::-1] or [crop]

    def split_spans(self, crop) -> list[list[int]]:
        """حدود الكلمات [x0, x1] من اليسار لليمين (انظر split_words)."""
        np, cv2 = self.np, self.cv2
        h, w = crop.shape[:2]
        if w < h * 2:
            return [[0, w]]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        _t, ink = cv2.threshold(gray, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        cols = ink.sum(0) > 0
        rows = np.flatnonzero(ink.sum(1))
        ink_h = (rows[-1] - rows[0] + 1) if len(rows) else h
        gap = max(3, int(ink_h * GAP_RATIO))
        cuts, run = [], 0
        for x in range(w):
            if cols[x]:
                if run >= gap:
                    cuts.append(x - run // 2)
                run = 0
            else:
                run += 1
        edges = [0] + cuts + [w]
        spans = [[a, b] for a, b in zip(edges, edges[1:]) if b - a > 2 and cols[a:b].any()]
        # مقطع قصير جداً (أصفار هندية "٠" = نقاط، أو نقطة/فاصلة) ليس كلمة: يُضم لأقرب جار،
        # وإلا قُرئ وحده نقاطاً وانفصل عن رقمه (٤٣٠٠٠ ← "٤٣" + ". ·").
        def ink_height(a, b):
            r = np.flatnonzero(ink[:, a:b].sum(1))
            return (r[-1] - r[0] + 1) if len(r) else 0
        i = 0
        while len(spans) > 1 and i < len(spans):
            a, b = spans[i]
            if ink_height(a, b) < ink_h * 0.4:
                left = spans[i][0] - spans[i - 1][1] if i > 0 else 1 << 30
                right = spans[i + 1][0] - spans[i][1] if i + 1 < len(spans) else 1 << 30
                j = i - 1 if left <= right else i + 1
                spans[j] = [min(spans[j][0], a), max(spans[j][1], b)]
                del spans[i]
                i = max(0, i - 1)
                continue
            i += 1
        return spans or [[0, w]]

    # ---------------- الأرقام
    def _zero_dots(self, crop, chars):
        """الصفر الهندي "٠" نقطة صغيرة يتجاهلها النموذج غالباً (٢٠٢٥ ← "٢٥"، ١٩٨٠ ← "١٩٨": قيس على أحكام
        حقيقية). في مقطع أرقام فقط: كل مكوّن حبر صغير مستقل بين حدود الأرقام لم يُقرأ عنده "٠" يُدرج صفراً
        في موضعه. النقطة على خط القاعدة (نهاية جملة) لا تُحسب."""
        np, cv2 = self.np, self.cv2
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        _t, ink = cv2.threshold(gray, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        n, _lab, st, cen = cv2.connectedComponentsWithStats(ink.astype("uint8"), 8)
        comps = [(st[i, 0], st[i, 1], st[i, 2], st[i, 3], cen[i][0]) for i in range(1, n) if st[i, 4] >= 3]
        if not comps:
            return chars
        tall = max(c[3] for c in comps)
        big = [c for c in comps if c[3] >= tall * 0.5]
        H = float(np.median([c[3] for c in big]))
        if tall >= H * SLASH_TALL:   # "/" أطول من الأرقام بوضوح (قيس: 44 مقابل 24 بكسل): الوسيط بدونه
            H = float(np.median([c[3] for c in big if c[3] < H * SLASH_TALL] or [H]))
        top = float(np.median([c[1] for c in big]))
        bottom = float(np.median([c[1] + c[3] for c in big]))
        dots, slashes = [], []
        for x, y, w, h, cx in comps:
            if w <= H * 0.8 and (h >= H * SLASH_TALL or (h >= H * 0.75 and self._slanted(ink[y:y + h, x:x + w]))):
                # القوس "(" ")" طويل أيضاً: إن قُرئ عنده قوس أو "/" فلا تغيير
                if not any(abs(px - cx) <= H * 0.3 for px, ch, _c in chars if ch in "()/\\"):
                    slashes.append(cx)
                continue
            if h > H * ZERO_MAX or w > H * ZERO_MAX or h < 2:
                continue
            cy = y + h / 2
            if not (top + H * 0.15 <= cy <= bottom - H * 0.12):
                continue   # أعلى الرقم (همزة/علامة) أو على خط القاعدة (نقطة نهاية)
            if any(bx <= cx <= bx + bw for bx, _by, bw, _bh, _c in big):
                continue   # جزء من رقم أكبر
            dots.append(cx)
        n_digits = sum(1 for _x, ch, _c in chars if _DIGIT.match(ch))
        if len(dots) > 3 and len(dots) > n_digits:
            dots = []   # سطر نقاط (.......) لا أصفار
        extra = []
        for cx in slashes:
            # "/" إما ضاع تماماً أو قُرئ رقماً ("١" أو "٧"): يُستبدل الرقم الواقع عليه أو يُدرج
            hit = [c for c in chars if _DIGIT.match(c[1]) and abs(c[0] - cx) <= H * 0.3]
            if hit:
                chars = [c for c in chars if c is not hit[0]]
            extra.append((cx, "/", 0.5))
        if not dots and not extra:
            return chars
        zeros = [x for x, ch, _c in chars if ch == "٠"]
        for cx in dots:
            near = [z for z in zeros if abs(z - cx) <= H * 0.5]
            if near:
                zeros.remove(min(near, key=lambda z: abs(z - cx)))
                continue
            extra.append((cx, "٠", 0.5))
        # أي نقطة/فاصلة قُرئت في موضع الصفر نفسه تُستبدل
        out = [c for c in chars if not (c[1] in ".·•" and any(abs(c[0] - x) <= H * 0.4 for x, z, _c in extra if z == "٠"))]
        return sorted(out + extra, key=lambda t: t[0])

    def _slanted(self, comp) -> bool:
        """"/" مائل: مركز الحبر في ربعه الأعلى يمين مركزه في ربعه الأسفل بوضوح. "١" قائم تقريباً."""
        np = self.np
        h = comp.shape[0]
        q = max(1, h // 4)
        top, bot = comp[:q], comp[-q:]
        if not top.any() or not bot.any():
            return False
        xs = np.arange(comp.shape[1])
        ct = (top.sum(0) * xs).sum() / top.sum()
        cb = (bot.sum(0) * xs).sum() / bot.sum()
        return (ct - cb) >= h * 0.28

    def read_number(self, crop) -> tuple[str, float]:
        """مقطع أرقام (تاريخ، رقم قضية، مبلغ) يُقرأ قطعة واحدة بترتيبه (يسار←يمين) مع استرداد الأصفار.
        تقسيمه كلمات يقطعه عند فراغ الصفر ثم يعكس ترتيب القطع (٢٠٢٤/٩/١٧ ← "٢٤/٩/١٧ ٢٠")."""
        np, cv2 = self.np, self.cv2
        h = crop.shape[0]
        p = max(2, int(h * 0.25))
        crop = cv2.copyMakeBorder(crop, 0, 0, p, p, cv2.BORDER_REPLICATE)
        chars = self._zero_dots(crop, self._run_rec([crop])[0])
        real = [c for _x, ch, c in chars if ch.strip()]
        text = re.sub(r"\s+", "", pred_reverse(fix_indic_zeros("".join(ch for _x, ch, _c in chars))))
        return text, (sum(real) / len(real) if real else 0.0)

    def fix_numbers(self, crop, spans, recs):
        """recs: قراءة كل كلمة (من اليمين، كما في split_words). الكلمات المتجاورة التي كلها أرقام وفواصل تُضم
        وتُقرأ قطعة واحدة بـ read_number. يعيد القائمة الجديدة (من اليمين) وعدد مقاطع الأرقام المعادة."""
        ltr = recs[::-1]
        out, k, fixed = [], 0, 0
        while k < len(ltr):
            if not _NUMERIC.match(ltr[k][0]):
                out.append(ltr[k])
                k += 1
                continue
            j = k
            while j + 1 < len(ltr) and _NUMERIC.match(ltr[j + 1][0]):
                j += 1
            a, b = spans[k][0], spans[j][1]
            text, conf = self.read_number(crop[:, a:b])
            if _DIGIT.search(text):
                out.append((text, min(conf, min(c for _t, c in ltr[k:j + 1]))))
                fixed += 1
            else:
                out += ltr[k:j + 1]
            k = j + 1
        return out[::-1], fixed

    def ocr(self, png: bytes, scale: int = 1) -> list[dict]:
        np, cv2 = self.np, self.cv2
        img = cv2.imdecode(np.frombuffer(png, "uint8"), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("صورة غير صالحة")
        if scale > 1:
            # صورة المسح الأصلية منخفضة الدقة (فاكس ~100 نقطة أبيض/أسود): تكبير ناعم بدل حواف السلّم.
            # قيس على مذكرة حقيقية: أرقام صحيحة 3/6 بدل 1/6، وكلمات 9/15 بدل 5/15.
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            img = cv2.GaussianBlur(img, (0, 0), scale * 0.45)
        img, self.last_skew = self.deskew(img)
        self.last_size = [int(img.shape[1]), int(img.shape[0])]
        boxes = self.detect(img)
        if not boxes:
            return []
        # نموذج العربية يُسقط الأرقام داخل الأسطر الطويلة المختلطة (وتبقى ثقته عالية)، بينما يقرؤها
        # سليمة كلمةً كلمة: نقسم كل سطر عند الفراغات بين الكلمات ونقرأ الكلمات (يمين←يسار).
        words, owner, crops, spans = [], [], [], []
        for i, b in enumerate(boxes):
            c = self.crop(img, b)
            sp = self.split_spans(c)
            crops.append(c)
            spans.append(sp)
            for a, e in sp[::-1]:
                words.append(c[:, a:e])
                owner.append(i)
        recs = self.recognize(words)
        per = [[] for _ in boxes]
        for i, r in zip(owner, recs):
            per[i].append(r)
        for i in range(len(boxes)):
            if len(per[i]) == len(spans[i]) and any(_DIGIT.search(t) for t, _c in per[i]):
                per[i], _n = self.fix_numbers(crops[i], spans[i], per[i])
            per[i] = [r for r in per[i] if r[0]]
        lines = []
        for b, rs, line_img in zip(boxes, per, crops):
            if not rs:
                continue
            text = " ".join(t for t, _ in rs)
            conf = sum(c * len(t) for t, c in rs) / sum(len(t) for t, _ in rs)
            ln = {"text": text, "conf": round(conf, 4), "word_conf": [round(c, 4) for _, c in rs],
                  "box": [float(b[:, 0].min()), float(b[:, 1].min()), float(b[:, 0].max()), float(b[:, 1].max())]}
            if _DIGIT.search(text):
                ln["strip"] = self.strip_png(line_img)
            lines.append(ln)
        return lines

    def strip_png(self, crop) -> str:
        """صورة صغيرة (رمادية، ارتفاع STRIP_H) لمقطع فيه أرقام: إن اختلفت أرقام الصفحة بين القراءتين يُرسل
        هذا الشريط وحده لنموذج الرؤية بدل الصفحة كاملة."""
        cv2 = self.cv2
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        h, w = g.shape[:2]
        if h > STRIP_H:
            g = cv2.resize(g, (max(1, int(w * STRIP_H / h)), STRIP_H), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".png", g)
        return base64.b64encode(buf.tobytes()).decode("ascii") if ok else ""


def _send(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    block_network()
    root = argv[argv.index("--root") + 1]
    lib = os.path.join(root, "lib")
    sys.path.insert(0, lib)
    if hasattr(os, "add_dll_directory"):
        for sub in ("", "onnxruntime/capi", "cv2", "numpy.libs"):
            d = os.path.join(lib, sub)
            if os.path.isdir(d):
                os.add_dll_directory(d)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    try:
        engine = Engine(root)
    except Exception as e:
        _send({"ready": False, "error": f"{type(e).__name__}: {e}"})
        return 1
    _send({"ready": True, "pid": os.getpid()})
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except ValueError:
            _send({"ok": False, "error": "طلب غير مفهوم"})
            continue
        if req.get("op") == "quit":
            break
        t0 = time.perf_counter()
        try:
            lines = engine.ocr(base64.b64decode(req["png"]), int(req.get("scale") or 1))
            _send({"id": req.get("id"), "ok": True, "lines": lines, "ms": int((time.perf_counter() - t0) * 1000),
                   "size": getattr(engine, "last_size", None), "skew": getattr(engine, "last_skew", 0.0)})
        except Exception as e:
            _send({"id": req.get("id"), "ok": False, "error": f"{type(e).__name__}: {e}"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
