"""المحرّك المحلي: llama-server.exe (نسخة CPU) في الخلفية على 127.0.0.1 فقط.

- المنفذ عشوائي حر يختاره نظام التشغيل.
- يُغلق الخادم عند close()، وعند خروج البرنامج (atexit)، وإذا قُتل البرنامج فجأة
  يُغلقه ويندوز تلقائياً لأنه مربوط بـ Job Object (KILL_ON_JOB_CLOSE).
- العميل يرفض أي عنوان غير 127.0.0.1 حرفياً، ولا يستخدم أي proxy من النظام
  (proxy قد يحوّل الطلب إلى خارج الجهاز).
لهذا لا يمر هذا المحرّك عبر cloud_gate: الصور لا تغادر الجهاز.
"""

from __future__ import annotations

import atexit
import base64
import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from ..digits import DIGITS_RULE
from .base import EngineUnavailable, OcrRequest, PageResult, Provider

log = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"

PROMPT = (
    "Transcribe this document page into Markdown exactly as written. "
    "The text is mostly Arabic: keep Arabic words and their letter order exactly, do not translate, "
    "do not summarize, do not add commentary. Use Markdown headings for titles and Markdown tables for tables. "
    "Output only the Markdown. "
    + DIGITS_RULE
)


def check_loopback_url(url: str) -> str:
    """يقبل فقط http://127.0.0.1:<port> — لا localhost ولا ::1 ولا أي عنوان آخر."""
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname != LOOPBACK or parts.username or parts.password:
        raise ValueError(f"المحرّك المحلي يقبل فقط http://{LOOPBACK}:<port>، وليس: {url!r}")
    netloc_host = parts.netloc.rsplit(":", 1)[0]
    if netloc_host != LOOPBACK or parts.port is None:
        raise ValueError(f"عنوان غير مقبول للمحرّك المحلي: {url!r}")
    return f"http://{LOOPBACK}:{parts.port}"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((LOOPBACK, 0))
        return s.getsockname()[1]


def server_command(exe: Path, model: Path, mmproj: Path, port: int, threads: int | None = None,
                   ctx_size: int = 8192) -> list[str]:
    cmd = [
        str(exe), "-m", str(model), "--mmproj", str(mmproj),
        "--host", LOOPBACK, "--port", str(port),
        "--ctx-size", str(ctx_size), "--parallel", "1",
    ]
    if threads:
        cmd += ["--threads", str(threads)]
    return cmd


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _kill_on_close_job():
    """Job Object: إذا انتهى البرنامج (حتى بالقتل) يُغلق ويندوز الخادم معه."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        return k32, job
    except Exception:
        log.warning("تعذّر إنشاء Job Object؛ الإغلاق سيعتمد على atexit فقط", exc_info=True)
        return None


class LlamaServer:
    """يشغّل llama-server ويغلقه. استخدمه مع with."""

    def __init__(self, exe: Path, model: Path, mmproj: Path, log_dir: Path, threads: int | None = None,
                 startup_timeout: float = 600):
        self.exe, self.model, self.mmproj = Path(exe), Path(model), Path(mmproj)
        self.log_path = Path(log_dir) / "llama-server.log"
        self.threads = threads
        self.startup_timeout = startup_timeout
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self._job = None
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    def start(self, stop: threading.Event | None = None) -> str:
        for f in (self.exe, self.model, self.mmproj):
            if not f.is_file():
                raise EngineUnavailable(f"ملف غير موجود: {f}")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):  # المنفذ قد يُحجز بين اختياره والتشغيل
            self.port = free_port()
            cmd = server_command(self.exe, self.model, self.mmproj, self.port, self.threads)
            log.info("تشغيل llama-server على %s", self.base_url)
            logf = open(self.log_path, "ab")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         creationflags=flags, cwd=str(self.exe.parent))
            logf.close()
            atexit.register(self.close)
            self._attach_job()
            try:
                self._wait_ready(stop)
                return self.base_url
            except EngineUnavailable:
                code = self.proc.poll()
                self.close()
                if code is None or attempt == 2 or "bind" not in self._log_tail().lower():
                    raise
        raise EngineUnavailable("تعذّر تشغيل llama-server")

    def _attach_job(self) -> None:
        if self._job is None:
            self._job = _kill_on_close_job()
        if self._job and self.proc:
            try:
                import ctypes
                k32, job = self._job
                k32.AssignProcessToJobObject(job, int(self.proc._handle))
            except Exception:
                log.warning("تعذّر ربط الخادم بـ Job Object", exc_info=True)

    def _log_tail(self, n: int = 1500) -> str:
        try:
            data = self.log_path.read_bytes()[-n:]
            return data.decode("utf-8", "replace")
        except OSError:
            return ""

    def _wait_ready(self, stop: threading.Event | None) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                raise EngineUnavailable("أُلغي تشغيل المحرّك")
            if self.proc.poll() is not None:
                raise EngineUnavailable(f"توقف llama-server أثناء التشغيل (رمز {self.proc.returncode}):\n{self._log_tail()}")
            try:
                with _NO_PROXY_OPENER.open(self.base_url + "/health", timeout=2) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass  # 503 أثناء تحميل النموذج
            time.sleep(0.5)
        raise EngineUnavailable("انتهت مهلة تشغيل llama-server")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        with self._lock:
            p, self.proc = self.proc, None
        if p and p.poll() is None:
            log.info("إغلاق llama-server")
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)
        try:
            atexit.unregister(self.close)
        except Exception:
            pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()


class LocalLlamaProvider(Provider):
    """عميل HTTP لخادم llama-server محلي (واجهة /v1/chat/completions)."""

    is_external = False

    def __init__(self, base_url: str, name: str = "local", server: LlamaServer | None = None,
                 timeout: float = 900, max_tokens: int = 4096):
        self.base_url = check_loopback_url(base_url)
        self.name = name
        self.server = server
        self.timeout = timeout
        self.max_tokens = max_tokens

    def _post(self, path: str, body: dict) -> dict:
        url = check_loopback_url(self.base_url) + path  # تحقق ثانٍ قبل كل طلب
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with _NO_PROXY_OPENER.open(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError) as e:
            if self.server is not None and not self.server.alive():
                raise EngineUnavailable(f"توقف المحرّك المحلي: {e}") from e
            raise

    def ocr(self, request: OcrRequest) -> list[PageResult]:
        results = []
        for img in request.images:
            text = PROMPT
            if request.previous_tail:
                text += "\n\nFor context only (do not repeat), the previous page ended with:\n" + request.previous_tail
            content = [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(img.png).decode()}},
                {"type": "text", "text": text},
            ]
            t0 = time.monotonic()
            data = self._post("/v1/chat/completions", {
                "messages": [{"role": "user", "content": content}],
                "temperature": 0, "max_tokens": self.max_tokens, "stream": False,
                # DRY: يعاقب تكرار نفس التسلسل (حلقات "ملا ملا ملا") دون إفساد التكرار الطبيعي القصير
                "dry_multiplier": 0.8, "dry_base": 1.75, "dry_allowed_length": 3,
                "repeat_penalty": 1.05, "repeat_last_n": 128,
            })
            md = _strip_fence(data["choices"][0]["message"]["content"] or "")
            meta = {"seconds": round(time.monotonic() - t0, 1), "dpi": img.dpi,
                    "finish": data["choices"][0].get("finish_reason")}
            results.append(PageResult(img.page_number, md, meta))
        return results

    def close(self) -> None:
        if self.server is not None:
            self.server.close()


def _strip_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        s = s[first_nl + 1:] if first_nl != -1 else ""
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


def start_local_provider(exe: Path, model: Path, mmproj: Path, log_dir: Path, name: str,
                         stop: threading.Event | None = None, threads: int | None = None) -> LocalLlamaProvider:
    server = LlamaServer(exe, model, mmproj, log_dir, threads)
    url = server.start(stop)
    try:
        return LocalLlamaProvider(url, name=name, server=server)
    except Exception:
        server.close()
        raise
