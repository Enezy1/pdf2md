"""PaddleOCR (تجريبي): محرّك OCR اختياري وقابل للحذف بالكامل.

الطريقة: نماذج PP-OCRv5 الرسمية بعد تحويلها إلى ONNX (من مستودع RapidOCR، نفس البصمات المنشورة في
default_models.yaml لإصدار 3.10.0) وتشغيلها بـ ONNX Runtime على المعالج. لا paddlepaddle ولا paddleocr.
  - كشف النص: ch_PP-OCRv5_det_mobile (متعدد اللغات)
  - التعرّف: arabic_PP-OCRv5_rec_mobile (القاموس مضمّن داخل ملف ONNX)

العزل: كل شيء في %APPDATA%\\pdf2md\\engines\\paddle\\
  downloads\\   الملفات المنزّلة (wheel/onnx) — تُستأنف وتُتحقق بالحجم وSHA-256
  lib\\         المكتبات (فك ملفات wheel مثبّتة الإصدار) — لا تلمس مكتبات البرنامج ولا requirements.txt
  models\\      النموذجان
  version.json  يُكتب آخراً؛ وجوده مع تطابق الإصدار = "مثبّت"
التشغيل في عملية منفصلة (app/paddle_worker.py) عبر stdin/stdout بـ JSON، والشبكة ممنوعة داخلها.
لا يمر عبر cloud_gate لأنه محلي بالكامل.

يتطلب تشغيل البرنامج بـ python main.py (بايثون 3.13 / 64-بت، لأن ملفات wheel مثبّتة لهذا الإصدار).
داخل exe (PyInstaller) لا يوجد مفسّر بايثون لتشغيل العملية المنفصلة، فالمحرّك غير متاح هناك حالياً.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import model_manager as mm
from .config import app_data_dir
from .digits import normalize_digits
from .models_catalog import FileSpec

log = logging.getLogger(__name__)

ENGINE_VERSION = "ppocrv5-onnx-1"
PYTHON_TAG = (3, 13)
RENDER_DPI = 300          # النموذج العربي يقرأ أدق كلما كبرت الحروف (تم قياسه: 200 تضيّع أرقاماً)
METHOD = "ocr:paddle"
LOW_MARK = "[؟]"          # علامة السطر منخفض الثقة (تعدّها page_score.bracket_marks فتدخل تقرير المراجعة)

_MS = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5"
_PY = "https://files.pythonhosted.org/packages"


@dataclass(frozen=True)
class Item:
    kind: str        # "wheel" أو "model"
    url: str
    file: FileSpec


# الأحجام والبصمات من PyPI JSON API ومن default_models.yaml في rapidocr 3.10.0 (تحقق 2026-10-10).
ITEMS: tuple[Item, ...] = (
    Item("model", f"{_MS}/det/ch_PP-OCRv5_det_mobile.onnx",
         FileSpec("ch_PP-OCRv5_det_mobile.onnx", 4819576,
                  "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae")),
    Item("model", f"{_MS}/rec/arabic_PP-OCRv5_rec_mobile.onnx",
         FileSpec("arabic_PP-OCRv5_rec_mobile.onnx", 8023828,
                  "c1192e632d0baa9146ae5b756a0e635e3dc63c1733737ebfd1629e87144e9295")),
    Item("wheel", f"{_PY}/ba/42/0a1920d276a0e1ca21dc0d13ee9e3ba10a9a8aa3abac76cd5e5a9f503306/pyclipper-1.4.0-cp313-cp313-win_amd64.whl",
         FileSpec("pyclipper-1.4.0-cp313-cp313-win_amd64.whl", 104007,
                  "d4b2d7c41086f1927d14947c563dfc7beed2f6c0d9af13c42fe3dcdc20d35832")),
    Item("wheel", f"{_PY}/f3/ec/100f2b1794ede74a9b3d7ec6b9736927f56713414c1dfe19ab6c383494bf/numpy-2.5.3-cp313-cp313-win_amd64.whl",
         FileSpec("numpy-2.5.3-cp313-cp313-win_amd64.whl", 12560965,
                  "71cad2b2a7451ab79d8f5e71b453485b6775963d5cf794179144a7463fe6e8ec")),
    Item("wheel", f"{_PY}/c4/86/05ed2056f43b27aaf12ebc592ebd9037a26bed315958cf882f43425fd469/onnxruntime-1.31.0-cp313-cp313-win_amd64.whl",
         FileSpec("onnxruntime-1.31.0-cp313-cp313-win_amd64.whl", 14888306,
                  "73e0165d58ece068c2a8a1c477c90b38e5a8adbbd399fdfdfd4bd79cbc28ff8d")),
    Item("wheel", f"{_PY}/b8/88/763b967f7efd7226b82c9fae16d560cba049b1f0c036647e65c610fd636e/opencv_python_headless-5.0.0.93-cp37-abi3-win_amd64.whl",
         FileSpec("opencv_python_headless-5.0.0.93-cp37-abi3-win_amd64.whl", 43825962,
                  "829717b6a95554f273e49e357cee3b3a2a26b6f4842fbc1bed2b45bdd8f87e0e")),
)


def total_size() -> int:
    return sum(i.file.size for i in ITEMS)


class PaddleError(Exception):
    pass


# ------------------------------------------------------------ المسارات والحالة

def engines_root() -> Path:
    return app_data_dir() / "engines"


def root() -> Path:
    return engines_root() / "paddle"


def version_file() -> Path:
    return root() / "version.json"


def worker_script() -> Path:
    return Path(__file__).with_name("paddle_worker.py")


def unsupported_reason() -> str:
    """"" إذا أمكن تشغيل المحرّك في هذه البيئة، وإلا السبب."""
    if getattr(sys, "frozen", False):
        return "غير متاح في نسخة exe: يحتاج تشغيل البرنامج بـ python main.py."
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        return "يتطلب ويندوز 64-بت."
    if sys.version_info[:2] != PYTHON_TAG:
        return f"يتطلب بايثون {PYTHON_TAG[0]}.{PYTHON_TAG[1]} (الحالي {sys.version_info[0]}.{sys.version_info[1]})."
    return ""


def is_installed() -> bool:
    try:
        v = json.loads(version_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if v.get("version") != ENGINE_VERSION:
        return False
    return all((root() / "models" / i.file.name).is_file() for i in ITEMS if i.kind == "model")


def is_available() -> bool:
    return is_installed() and not unsupported_reason()


def installed_size() -> int:
    r = root()
    return sum(f.stat().st_size for f in r.rglob("*") if f.is_file()) if r.is_dir() else 0


# ------------------------------------------------------------ التنصيب والحذف

def _download_dir() -> Path:
    return root() / "downloads"


def _finish_install(progress: mm.Progress | None) -> None:
    """يفك ملفات wheel إلى lib وينقل النموذجين، ثم يكتب version.json (آخر خطوة)."""
    r, dl = root(), _download_dir()
    lib_tmp = r / "lib.tmp"
    shutil.rmtree(lib_tmp, ignore_errors=True)
    for i in ITEMS:
        if i.kind != "wheel":
            continue
        if progress:
            progress(f"فك {i.file.name}", 0, 0)
        with zipfile.ZipFile(dl / i.file.name) as z:
            base = lib_tmp.resolve()
            for info in z.infolist():
                p = (lib_tmp / info.filename).resolve()
                if not str(p).startswith(str(base)):
                    raise mm.IntegrityError(f"مسار غير آمن داخل {i.file.name}: {info.filename}")
            z.extractall(lib_tmp)
    shutil.rmtree(r / "lib", ignore_errors=True)
    os.replace(lib_tmp, r / "lib")
    (r / "models").mkdir(exist_ok=True)
    for i in ITEMS:
        if i.kind == "model":
            shutil.copyfile(dl / i.file.name, r / "models" / i.file.name)
    from .fsutil import atomic_write_json
    atomic_write_json(version_file(), {
        "version": ENGINE_VERSION, "python": f"{PYTHON_TAG[0]}.{PYTHON_TAG[1]}",
        "files": {i.file.name: i.file.sha256 for i in ITEMS}})
    # الملفات المنزّلة لم تعد لازمة (lib وmodels تكفي)
    shutil.rmtree(dl, ignore_errors=True)
    log.info("PaddleOCR: اكتمل التنصيب في %s", r)


def _check_supported() -> None:
    why = unsupported_reason()
    if why:
        raise PaddleError(why)


def install(progress: mm.Progress | None = None, stop: threading.Event | None = None, opener=None) -> str:
    """ينزّل كل الملفات (مع الاستئناف والتحقق) ثم يفكها. لا شيء يُنزَّل وقت التشغيل."""
    _check_supported()
    for i in ITEMS:
        mm.download_file(i.url, _download_dir() / i.file.name, i.file, progress, stop, opener)
    _finish_install(progress)
    return f"تم تنصيب PaddleOCR ({installed_size() / 1e6:.0f}MB) في {root()}"


def files_in(folder: Path) -> bool:
    """هل في المجلد أي ملف من ملفات PaddleOCR؟"""
    return any(mm._find(Path(folder), i.file.name) for i in ITEMS)


def install_from_folder(folder: Path, progress: mm.Progress | None = None, stop: threading.Event | None = None) -> str:
    """نسخ الملفات نفسها من مجلد (فلاشة) — نفس أسماء الملفات، والبصمة تُتحقق أثناء النسخ."""
    _check_supported()
    folder = Path(folder)
    missing = []
    for i in ITEMS:
        dest = _download_dir() / i.file.name
        if mm.is_verified(dest, i.file):
            continue
        src = mm._find(folder, i.file.name)
        if src is None:
            missing.append(i.file.name)
            continue
        mm.copy_verified(src, dest, i.file, progress, stop)
    if missing:
        raise PaddleError("ملفات ناقصة في المجلد:\n" + "\n".join(missing))
    _finish_install(progress)
    return f"تم تنصيب PaddleOCR من المجلد ({installed_size() / 1e6:.0f}MB)"


def uninstall(settings=None) -> int:
    """يمسح engines\\paddle بالكامل ويعيد إعداداته للافتراضي. لا يلمس أي شيء آخر. يعيد البايتات المحررة."""
    r = root()
    eng = engines_root().resolve()
    target = r.resolve() if r.exists() else r
    if r.exists() and target.parent != eng:
        raise ValueError(f"مسار غير متوقع: {target}")
    freed = installed_size()
    if r.exists():
        shutil.rmtree(r)
    try:
        engines_root().rmdir()  # فقط إن كان فارغاً
    except OSError:
        pass
    if settings is not None:
        from .config import Settings
        d = Settings()
        settings.scan_engine = d.scan_engine
        settings.paddle_min_conf = d.paddle_min_conf
        settings.save()
    log.info("PaddleOCR: حُذف (%d بايت)", freed)
    return freed


# ------------------------------------------------------------ العملية المنفصلة

class PaddleWorker:
    """عميل العملية المنفصلة. أي تعطّل فيها يرفع PaddleError ولا يمس البرنامج."""

    def __init__(self, engine_root: Path | None = None, python: str | None = None, script: Path | None = None,
                 start_timeout: float = 120, page_timeout: float = 300):
        self.engine_root = Path(engine_root or root())
        self.python = python or sys.executable
        self.script = Path(script or worker_script())
        self.start_timeout, self.page_timeout = start_timeout, page_timeout
        self.proc: subprocess.Popen | None = None
        self._out: queue.Queue = queue.Queue()
        self._id = 0
        self._lock = threading.Lock()
        self.name = "paddle"

    def start(self) -> "PaddleWorker":
        cmd = [self.python, "-I", "-S", str(self.script), "--root", str(self.engine_root)]
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTHON", "HTTP_PROXY", "HTTPS_PROXY"))}
        env["PYTHONIOENCODING"] = "utf-8"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     cwd=str(self.engine_root), env=env, creationflags=flags)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        msg = self._get(self.start_timeout)
        if not msg.get("ready"):
            self.close()
            raise PaddleError(f"تعذّر تشغيل PaddleOCR: {msg.get('error', 'رد غير متوقع')}")
        self.info = msg
        return self

    def _reader(self, proc) -> None:
        for raw in proc.stdout:
            try:
                self._out.put(json.loads(raw.decode("utf-8")))
            except ValueError:
                continue
        self._out.put(None)  # انتهت العملية

    def _get(self, timeout: float) -> dict:
        try:
            msg = self._out.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise PaddleError("انتهت مهلة PaddleOCR")
        if msg is None:
            self.close()
            raise PaddleError("توقفت عملية PaddleOCR")
        return msg

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ocr_lines(self, png: bytes, scale: int = 1) -> tuple[list[dict], int]:
        with self._lock:
            if not self.alive:
                raise PaddleError("عملية PaddleOCR غير شغالة")
            self._id += 1
            req = json.dumps({"id": self._id, "op": "ocr", "png": base64.b64encode(png).decode("ascii"),
                              "scale": scale}) + "\n"
            try:
                self.proc.stdin.write(req.encode("utf-8"))
                self.proc.stdin.flush()
            except OSError as e:
                self.close()
                raise PaddleError(f"انقطع الاتصال بـ PaddleOCR: {e}")
            msg = self._get(self.page_timeout)
            if not msg.get("ok"):
                raise PaddleError(msg.get("error") or "فشل PaddleOCR")
            self.last_size = msg.get("size")
            self.last_skew = float(msg.get("skew") or 0.0)
            return msg.get("lines") or [], int(msg.get("ms") or 0)

    def close(self) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            if p.poll() is None:
                p.stdin.write(b'{"op": "quit"}\n')
                p.stdin.flush()
                p.wait(timeout=3)
        except Exception:
            pass
        if p.poll() is None:
            p.kill()
        for f in (p.stdin, p.stdout):
            try:
                f.close()
            except OSError:
                pass
        try:
            p.wait(timeout=3)
        except Exception:
            pass


# ------------------------------------------------------------ ترتيب الأسطر والتنسيق

@dataclass
class Line:
    text: str
    conf: float
    box: tuple[float, float, float, float]   # x0, y0, x1, y1
    strip: bytes | None = None               # صورة المقطع إن كان فيه أرقام (PNG رمادي) — انظر strips()


def _concat_strips(parts: list[bytes]) -> bytes | None:
    """أشرطة مقاطع السطر الواحد جنباً إلى جنب بترتيبها في الصورة (يسار←يمين)."""
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return parts[0] if parts else None
    import io

    from PIL import Image
    ims = [Image.open(io.BytesIO(p)).convert("L") for p in parts]
    h = max(i.height for i in ims)
    gap = h // 2
    out = Image.new("L", (sum(i.width for i in ims) + gap * (len(ims) - 1), h), 255)
    x = 0
    for i in ims:
        out.paste(i, (x, (h - i.height) // 2))
        x += i.width + gap
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()


def order_lines(items: list[dict]) -> list[Line]:
    """ترتيب القراءة العربي: أعلى←أسفل، وداخل السطر الواحد يمين←يسار. القطع المتجاورة أفقياً
    في نفس السطر (تداخل رأسي أكثر من النصف) تُدمج في سطر واحد، والثقة = أدنى ثقة بينها."""
    segs = sorted((Line(str(i["text"]).strip(), float(i.get("conf", 0)), tuple(i["box"]), _b64(i.get("strip")))
                   for i in items if str(i.get("text", "")).strip()), key=lambda s: (s.box[1] + s.box[3]) / 2)
    rows: list[list[Line]] = []
    for s in segs:
        h = s.box[3] - s.box[1]
        for row in rows:
            r0, r1 = min(x.box[1] for x in row), max(x.box[3] for x in row)
            overlap = min(r1, s.box[3]) - max(r0, s.box[1])
            if overlap > 0.5 * min(h, r1 - r0):
                row.append(s)
                break
        else:
            rows.append([s])
    rows.sort(key=lambda r: min(x.box[1] for x in r))
    out = []
    for row in rows:
        row.sort(key=lambda x: -x.box[2])
        out.append(Line(" ".join(x.text for x in row), min(x.conf for x in row),
                        (min(x.box[0] for x in row), min(x.box[1] for x in row),
                         max(x.box[2] for x in row), max(x.box[3] for x in row)),
                        _concat_strips([x.strip for x in reversed(row)])))
    return out


def _b64(s) -> bytes | None:
    try:
        return base64.b64decode(s) if s else None
    except (ValueError, TypeError):
        return None


MARGIN = 0.12


def drop_margin_marks(lines: list["Line"], size) -> list["Line"]:
    """سطر كامل من حرف أو حرفين على حافة الصفحة (رقم الصفحة "- ٢ -"، علامات التدبيس والختم) ليس من المتن.
    يُطبَّق على السطر بعد الدمج لا على المقطع، وإلا ضاعت أول كلمة قصيرة في السطر ("إن"، "من")."""
    if not size:
        return lines
    w, h = size
    out = []
    for i in lines:
        visible = re.sub(r"[\s\-–—_.·•|()\[\]{}]", "", i.text)
        x0, y0, x1, y1 = i.box
        edge = x1 < w * MARGIN or x0 > w * (1 - MARGIN) or y1 < h * MARGIN or y0 > h * (1 - MARGIN)
        if len(visible) <= 2 and edge:
            continue
        out.append(i)
    return out


def clean_line(text: str) -> str:
    """توحيد الأرقام (المركزي) وإصلاح الأقواس المعكوسة، كما في استخراج طبقة النص."""
    from .pdf_io import fix_mirrored_brackets
    text = normalize_digits(text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return fix_mirrored_brackets(text)


def to_markdown(lines: list[Line], min_conf: float, mark_low: bool) -> tuple[str, list[int]]:
    """النص النهائي للصفحة وأرقام الأسطر منخفضة الثقة (من 1). mark_low: تعليمها بـ [؟]."""
    out, low = [], []
    for k, ln in enumerate(lines, 1):
        t = clean_line(ln.text)
        if ln.conf < min_conf:
            low.append(k)
            if mark_low:
                t = f"{LOW_MARK} {t}"
        out.append(t)
    return "\n\n".join(out), low


def raw_text(lines: list[Line], ms: int = 0) -> str:
    """محتوى pNNNN.paddle.md: النص كما خرج مرتّباً، مع ثقة كل سطر (بلا توحيد أرقام: مرجع خام)."""
    head = f"<!-- engine: paddle {ENGINE_VERSION} | ms: {ms} | lines: {len(lines)} -->"
    return "\n".join([head] + [f"{ln.conf:.3f}\t{ln.text}" for ln in lines]) + "\n"


@dataclass
class PageOcr:
    markdown: str
    raw: str
    lines: list[Line]
    low_lines: list[int]
    min_conf: float
    ms: int
    strips: dict[int, bytes] = None          # {رقم السطر من 0 في markdown.split("\n\n"): صورة أرقامه}


POOR_SCAN_DPI = 150   # مسح أقل من هذا (فاكس ~100 نقطة) + أرقام في الصفحة ⇒ الأرقام غير موثوقة (قرار المستخدم، ثابت)
_DIGITS = re.compile(r"[0-9٠-٩۰-۹]")


def scan_dpi(page) -> float | None:
    """دقة صورة المسح الأصلية إن كانت الصفحة صورة واحدة تغطيها، وإلا None."""
    import pymupdf as fitz
    try:
        infos = page.get_image_info(xrefs=True)
        if len(infos) == 1 and infos[0].get("width"):
            bbox = fitz.Rect(infos[0]["bbox"])
            if bbox.width > 0 and bbox.get_area() >= 0.9 * page.rect.get_area():
                return infos[0]["width"] / (bbox.width / 72)
    except Exception:
        pass
    return None


def poor_scan_with_digits(dpi: float | None, text: str) -> bool:
    """مسح رديء (أقل من POOR_SCAN_DPI) وفي ناتجه أرقام: الأرقام تحتاج محرّكاً أقوى أو مراجعة."""
    return dpi is not None and dpi < POOR_SCAN_DPI and bool(_DIGITS.search(text or ""))


def page_image(page) -> tuple[bytes, int]:
    """(PNG، معامل التكبير) لصفحة مصوّرة. إذا كانت الصفحة صورة مسح واحدة مستقيمة بدقة أقل من 250 نقطة،
    تُرسل الصورة الأصلية ويكبّرها worker تكبيراً ناعماً؛ وإلا تُرسم الصفحة بدقة RENDER_DPI."""
    import pymupdf as fitz
    try:
        infos = page.get_image_info(xrefs=True)
        if len(infos) == 1 and infos[0].get("xref"):
            info = infos[0]
            bbox = fitz.Rect(info["bbox"])
            t = info.get("transform") or (1, 0, 0, 1, 0, 0)
            straight = abs(t[1]) < 1e-3 and abs(t[2]) < 1e-3 and t[0] > 0 and t[3] > 0
            if straight and bbox.get_area() >= 0.9 * page.rect.get_area():
                pix = fitz.Pixmap(page.parent, info["xref"])
                dpi = pix.width / (bbox.width / 72)
                if dpi < 250:
                    if pix.alpha or pix.n not in (1, 3):
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    return pix.tobytes("png"), max(2, min(4, round(RENDER_DPI / max(dpi, 1))))
    except Exception as e:  # أي شيء غير متوقع: الرسم العادي
        log.debug("page_image: %s", e)
    from .pdf_io import render_page_png
    return render_page_png(page, RENDER_DPI), 1


def ocr_page(worker: PaddleWorker, png: bytes, min_conf: float, mark_low: bool, scale: int = 1) -> PageOcr:
    items, ms = worker.ocr_lines(png, scale)
    lines = drop_margin_marks(order_lines(items), getattr(worker, "last_size", None))
    md, low = to_markdown(lines, min_conf, mark_low)
    return PageOcr(md, raw_text(lines, ms), lines, low, min((ln.conf for ln in lines), default=0.0), ms,
                   {k: ln.strip for k, ln in enumerate(lines) if ln.strip})


# ------------------------------------------------------------ اختبار سريع

QUICK_LINES = ("محكمة الاستئناف العليا", "القضية رقم 153 لسنة 2024", "المبلغ المطلوب 45000 دينار")
QUICK_EXPECT = ("محكمة", "الاستئناف", "القضية", "153", "2024", "45000", "دينار")


def quick_test_image() -> bytes:
    """صورة نص عربي مع أرقام تُولَّد داخل البرنامج (PyMuPDF يشكّل العربية)، لا من مستندات المستخدم."""
    import pymupdf as fitz
    doc = fitz.open()
    try:
        page = doc.new_page(width=500, height=200)
        body = "<br>".join(QUICK_LINES)
        page.insert_htmlbox(fitz.Rect(20, 20, 480, 190),
                            f'<div dir="rtl" style="font-size:20px;line-height:1.8">{body}</div>')
        return page.get_pixmap(dpi=RENDER_DPI).tobytes("png")
    finally:
        doc.close()


def quick_test(worker_factory=None) -> dict:
    """يشغّل المحرّك على الصورة المولّدة. يعيد {ok, text, missing, seconds}."""
    t0 = time.monotonic()
    w = (worker_factory or (lambda: PaddleWorker().start()))()
    try:
        res = ocr_page(w, quick_test_image(), 0.0, False)
    finally:
        w.close()
    text = res.markdown
    missing = [x for x in QUICK_EXPECT if x not in text]
    return {"ok": not missing, "text": text, "missing": missing, "seconds": time.monotonic() - t0,
            "ocr_ms": res.ms}
