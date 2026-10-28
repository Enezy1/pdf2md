"""تنزيل النماذج ونسخها من فلاشة، مع التحقق من الحجم والبصمة.

- التنزيل يُكتب إلى <name>.part ويُستأنف من حيث توقف عبر HTTP Range.
- بعد اكتمال الحجم تُحسب بصمة SHA-256 وتُقارن بالكتالوج؛ عند عدم التطابق يُحذف الجزء.
- النسخ من فلاشة يحسب البصمة أثناء النسخ نفسه، ثم يتحقق قبل إعادة التسمية.
- الملف المكتمل يُرافقه <name>.verified (البصمة)؛ عند الفحص لاحقاً نقارن الحجم فقط.

البيانات في %APPDATA%\\pdf2md\\models\\<id>\\ و ...\\llama\\<tag>\\.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from . import models_catalog as cat
from .config import app_data_dir

log = logging.getLogger(__name__)

CHUNK = 1 << 20
Progress = Callable[[str, int, int], None]  # (اسم الملف، المنجز، الكلي)


class IntegrityError(Exception):
    pass


class Cancelled(Exception):
    pass


def models_root() -> Path:
    return app_data_dir() / "models"


def model_dir(spec: cat.ModelSpec) -> Path:
    return models_root() / spec.id


def _marker(path: Path) -> Path:
    return path.with_name(path.name + ".verified")


def is_verified(path: Path, f: cat.FileSpec) -> bool:
    try:
        return path.stat().st_size == f.size and _marker(path).read_text().strip() == f.sha256
    except OSError:
        return False


def _finish(part: Path, final: Path, f: cat.FileSpec, digest: str) -> None:
    if digest != f.sha256:
        part.unlink(missing_ok=True)
        raise IntegrityError(f"بصمة {f.name} لا تطابق المتوقع — حُذف الملف. (المتوقع {f.sha256[:12]}… والفعلي {digest[:12]}…)")
    os.replace(part, final)
    _marker(final).write_text(f.sha256)
    log.info("تم التحقق من %s", f.name)


def _sha256_of(path: Path, stop: threading.Event | None, progress: Progress | None, label: str, total: int) -> str:
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            if stop is not None and stop.is_set():
                raise Cancelled()
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(f"تحقق {label}", done, total)
    return h.hexdigest()


def download_file(url: str, dest: Path, f: cat.FileSpec, progress: Progress | None = None,
                  stop: threading.Event | None = None, opener=None) -> Path:
    """ينزّل ملفاً مع الاستئناف والتحقق. يعيد المسار النهائي."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if is_verified(dest, f):
        return dest
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have > f.size:
        part.unlink()
        have = 0
    opener = opener or urllib.request.build_opener()
    if have < f.size:
        req = urllib.request.Request(url, headers={"User-Agent": "pdf2md", **({"Range": f"bytes={have}-"} if have else {})})
        try:
            resp = opener.open(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code != 416:
                raise
            resp = None  # الخادم يقول إن الجزء مكتمل
        if resp is not None:
            with resp:
                if have and resp.status != 206:
                    log.info("الخادم لا يدعم الاستئناف لـ %s، يبدأ من الصفر", f.name)
                    have = 0
                with open(part, "ab" if have else "wb") as out:
                    while chunk := resp.read(CHUNK):
                        if stop is not None and stop.is_set():
                            raise Cancelled()
                        out.write(chunk)
                        have += len(chunk)
                        if progress:
                            progress(f.name, have, f.size)
                        if have > f.size:
                            break
    if part.stat().st_size != f.size:
        size = part.stat().st_size
        if size > f.size:
            part.unlink()
        raise IntegrityError(f"حجم {f.name} غير صحيح ({size} بدل {f.size}). أعد المحاولة للاستئناف.")
    _finish(part, dest, f, _sha256_of(part, stop, progress, f.name, f.size))
    return dest


def copy_verified(src: Path, dest: Path, f: cat.FileSpec, progress: Progress | None = None,
                  stop: threading.Event | None = None) -> Path:
    """ينسخ ملفاً (من فلاشة مثلاً) ويحسب بصمته أثناء النسخ."""
    if src.stat().st_size != f.size:
        raise IntegrityError(f"حجم {src.name} في المصدر غير صحيح ({src.stat().st_size} بدل {f.size}).")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    done = 0
    try:
        with open(src, "rb") as fi, open(part, "wb") as fo:
            while chunk := fi.read(CHUNK):
                if stop is not None and stop.is_set():
                    raise Cancelled()
                h.update(chunk)
                fo.write(chunk)
                done += len(chunk)
                if progress:
                    progress(f"نسخ {f.name}", done, f.size)
            fo.flush()
            os.fsync(fo.fileno())
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    _finish(part, dest, f, h.hexdigest())
    return dest


# ------------------------------------------------------------ النماذج

def model_paths(spec: cat.ModelSpec) -> tuple[Path, Path]:
    d = model_dir(spec)
    return d / spec.model.name, d / spec.mmproj.name


def is_installed(spec: cat.ModelSpec) -> bool:
    m, p = model_paths(spec)
    return is_verified(m, spec.model) and is_verified(p, spec.mmproj)


def installed_models() -> list[cat.ModelSpec]:
    return [m for m in cat.MODELS if is_installed(m)]


def download_model(spec: cat.ModelSpec, progress: Progress | None = None, stop: threading.Event | None = None) -> None:
    """ينزّل النموذج وملف mmproj معاً (الاثنان مطلوبان)."""
    m, p = model_paths(spec)
    download_file(spec.url(spec.mmproj), p, spec.mmproj, progress, stop)
    download_file(spec.url(spec.model), m, spec.model, progress, stop)


def _find(folder: Path, name: str) -> Path | None:
    direct = folder / name
    if direct.is_file():
        return direct
    for hit in folder.rglob(name):
        if hit.is_file():
            return hit
    return None


def import_from_folder(folder: Path, progress: Progress | None = None,
                       stop: threading.Event | None = None) -> dict:
    """يبحث في المجلد عن أي نموذج من الكتالوج (الملفان معاً) وعن zip الخاص بـ llama-server.

    يعيد {"models": [...], "llama": bool, "skipped": [سبب...]}.
    """
    folder = Path(folder)
    report = {"models": [], "llama": False, "skipped": []}
    for spec in cat.MODELS:
        src_m, src_p = _find(folder, spec.model.name), _find(folder, spec.mmproj.name)
        if not src_m and not src_p:
            continue
        if not (src_m and src_p):
            missing = spec.mmproj.name if src_m else spec.model.name
            report["skipped"].append(f"{spec.label}: ينقص الملف {missing}")
            continue
        dst_m, dst_p = model_paths(spec)
        # mmproj اسمه عام لدى بعض المستودعات (mmproj-model-f16.gguf) — البصمة تحسم لأي نموذج هو.
        try:
            copy_verified(src_p, dst_p, spec.mmproj, progress, stop)
            copy_verified(src_m, dst_m, spec.model, progress, stop)
            report["models"].append(spec.id)
        except IntegrityError as e:
            report["skipped"].append(f"{spec.label}: {e}")
    z = _find(folder, cat.LLAMA_ZIP.name)
    if z:
        try:
            dst = llama_root() / cat.LLAMA_ZIP.name
            copy_verified(z, dst, cat.LLAMA_ZIP, progress, stop)
            _extract_llama(dst)
            report["llama"] = True
        except IntegrityError as e:
            report["skipped"].append(f"llama-server: {e}")
    return report


# ------------------------------------------------------------ llama-server

def llama_root() -> Path:
    return app_data_dir() / "llama"


def _llama_dir(gpu: bool) -> Path:
    return llama_root() / (cat.LLAMA_TAG + ("-vulkan" if gpu else ""))


def llama_exe(gpu: bool = False) -> Path | None:
    """نسخة المعالج (الافتراضية) أو نسخة Vulkan لكرت الشاشة."""
    exe = _llama_dir(gpu) / "llama-server.exe"
    return exe if exe.is_file() else None


def engine_for(use_gpu: bool) -> tuple[Path | None, Path | None]:
    """(المحرّك المطلوب، محرّك المعالج الاحتياطي). إن طُلب الكرت ولم تُنزَّل نسخته يُستعمل المعالج."""
    cpu = llama_exe()
    if use_gpu:
        gpu = llama_exe(gpu=True)
        if gpu:
            return gpu, cpu
    return cpu, None


def _extract_llama(zip_path: Path, gpu: bool = False) -> Path:
    target = _llama_dir(gpu)
    tmp = target.with_name(target.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():  # حماية من مسارات خارج المجلد
            p = (tmp / info.filename).resolve()
            if not str(p).startswith(str(tmp.resolve())):
                raise IntegrityError(f"مسار غير آمن داخل الأرشيف: {info.filename}")
        z.extractall(tmp)
    # بعض الإصدارات تضع الملفات داخل مجلد فرعي
    exe = next(tmp.rglob("llama-server.exe"), None)
    if exe is None:
        shutil.rmtree(tmp, ignore_errors=True)
        raise IntegrityError("الأرشيف لا يحتوي llama-server.exe")
    shutil.rmtree(target, ignore_errors=True)
    os.replace(exe.parent, target)
    shutil.rmtree(tmp, ignore_errors=True)
    return target / "llama-server.exe"


def download_llama(progress: Progress | None = None, stop: threading.Event | None = None,
                   gpu: bool = False) -> Path:
    zip_spec, url = (cat.LLAMA_VULKAN_ZIP, cat.LLAMA_VULKAN_URL) if gpu else (cat.LLAMA_ZIP, cat.LLAMA_URL)
    zp = download_file(url, llama_root() / zip_spec.name, zip_spec, progress, stop)
    return _extract_llama(zp, gpu)


def installed_folders() -> list[tuple[str, str, int]]:
    """كل مجلدات النماذج على القرص (حتى غير الموجودة في الكتالوج): (المعرّف، الاسم، الحجم بالبايت)."""
    root = models_root()
    if not root.is_dir():
        return []
    labels = {m.id: m.label for m in cat.MODELS}
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        out.append((d.name, labels.get(d.name, f"{d.name} (لم يعد في الكتالوج)"), size))
    return out


def delete_model(model_id: str) -> int:
    """يحذف مجلد النموذج كاملاً. يعيد عدد البايتات المحررة."""
    import shutil
    root = models_root().resolve()
    target = (root / model_id).resolve()
    if target.parent != root or not target.is_dir():
        raise ValueError(f"مجلد نموذج غير صالح: {model_id!r}")
    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    shutil.rmtree(target)
    return size
