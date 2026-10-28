"""الصفحة الرئيسية: اختيار الملفات، الوضع (داخلي | عبر المواقع)، ابدأ، والنتيجة.

كل الإعدادات الأخرى في نافذة الإعدادات (settings_window). العمل الثقيل في خيوط خلفية
ترسل أحداثاً عبر queue، والواجهة تقرأها كل 100ms عبر after() — فلا تتجمد.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
import time

import customtkinter as ctk
import pymupdf as fitz

from .. import __version__
from .. import model_manager as mm
from .. import models_catalog as cat
from ..audit_log import AuditLog
from ..cloud_gate import CLOUD, LOCAL, CloudGate
from ..config import Settings, app_data_dir, cleanup_work_dir, setup_logging, work_dir
from ..file_options import FileOptions
from ..hashing import HashCache
from ..pipeline import BatchContext, Job, apply_review_decisions, build_index_safe, run_api_phase, run_batch
from ..providers.catalog import ProviderConfig
from ..rate_limit import UsageCounter
from ..secrets_store import SecretsStore
from . import ctk_ui as ui
from . import theme
from .theme import R, S1, S2, S3, S4, px

log = logging.getLogger(__name__)

OUTPUT_NAME = "_markdown"

STATUS_LABELS = {
    "done": "نص", "blank": "فارغة", "pending_engine": "بانتظار المحرّك", "error": "خطأ",
    "needs_review": "بانتظار المراجعة", "illegible": "غير مقروء", "unreliable": "غير موثوق", "todo": "لم تُعالج",
}


INPUT_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}  # الصور تُفتح كمستند من صفحة (أو أكثر لـ TIFF)


@dataclass
class FileEntry:
    path: Path
    pages: int | None = None
    file_hash: str | None = None
    status: str = ""
    error: str = ""
    extra: bool = False  # أُضيف من خارج المجلد
    size: int = 0


class BgTask:
    """تنزيل/نسخ نموذج في الخلفية. حالته هنا حتى تعرضها نافذة الإعدادات متى فُتحت."""

    def __init__(self):
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.title = ""
        self.label = ""
        self.done = 0
        self.total = 0
        self.result = ""
        self.error = ""
        # key: المحرّك الجاري تنزيله (يُعرض التقدم في صفه). measure: (المنجز، الكلي) من الملفات على القرص،
        # فتبقى النسبة صحيحة بعد الاستئناف. الإيقاف المؤقت يبقي الجزء المنزّل (.part)، والاستئناف يكمل منه.
        self.key = ""
        self.paused = False
        self._args = None
        self._measure = None
        self._speed: list[tuple[float, int]] = []

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, title: str, fn, key: str = "", measure=None) -> bool:
        if self.running:
            return False
        self.stop = threading.Event()
        self.title, self.label, self.done, self.total, self.result, self.error = title, "", 0, 0, "", ""
        self.key, self.paused, self._args, self._measure, self._speed = key, False, (title, fn, key, measure), measure, []

        def progress(name, done, total):
            self.label, self.done, self.total = name, done, total

        def work():
            try:
                self.result = fn(progress, self.stop)
            except mm.Cancelled:
                self.result = "" if self.paused else "أُلغي. يُستأنف التنزيل من حيث توقف عند الضغط على «تنزيل» مرة أخرى."
            except Exception as e:
                log.exception("فشل %s", title)
                self.error = str(e)

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        return True

    def pause(self) -> None:
        if self.running:
            self.paused = True
            self.stop.set()

    def resume(self) -> bool:
        if self.running or not self.paused or not self._args:
            return False
        return self.start(*self._args)

    def cancel(self) -> None:
        """إيقاف هذه المرة؛ الجزء المنزّل يبقى على القرص فيُستأنف لاحقاً بزر «تنزيل»."""
        self.stop.set()
        self.paused = False
        if not self.running:
            self.key = ""

    def overall(self) -> tuple[int, int, float | None]:
        """(المنجز، الكلي، الثواني المتبقية تقديراً) للعمل كله، لا للملف الحالي فقط."""
        done, total = self.done, self.total
        if self._measure is not None:
            try:
                done, total = self._measure()
            except OSError:
                pass
        now = time.monotonic()
        if self.running and not self.label.startswith("تحقق"):
            self._speed.append((now, done))
            self._speed = [x for x in self._speed if now - x[0] <= 15]
        eta = None
        if len(self._speed) >= 2 and total:
            (t0, d0), (t1, d1) = self._speed[0], self._speed[-1]
            if t1 > t0 and d1 > d0:
                eta = (total - done) / ((d1 - d0) / (t1 - t0))
        return done, total, eta


class MainWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = Settings.load()
        data = app_data_dir()
        self.hash_cache = HashCache(data / "hash_cache.json")
        self.file_options = FileOptions(data / "file_options.json")
        self.secrets = SecretsStore()
        self.provider_config = ProviderConfig()
        self.usage = UsageCounter(data / "usage.json")
        self.gate = CloudGate(AuditLog(data / "sent_log.csv"), self._gate_mode())
        self.bg = BgTask()
        self.pending_review: list[dict] = []
        self.checked: set[str] = set()
        self.events: queue.Queue = queue.Queue()
        self.files: dict[str, FileEntry] = {}
        self.scan_token = 0
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.running_jobs: list[Job] = []
        self.active_provider = None
        self.settings_win = None
        self._cur = {"index": 0, "page": 0, "pages": 1}
        self.pause_event = threading.Event()
        self._run = {"t0": 0.0, "done": set(), "total": 0, "stage": "", "files": 0}

        theme.apply(root, self.settings.theme, self.settings.palette)
        root.title(f"pdf2md {__version__}")  # شريط عنوان ويندوز لا يحترم RLE في النص المختلط
        theme.fit_window(root, px(900), px(760), px(720), px(560))
        self._build()
        self._update_count()
        self._fit_min_height()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll_events)
        root.after(3000, self._watch_folder)
        from . import clipboard
        clipboard.install(root)
        # اختصارات (برمز الزر الفعلي حتى تعمل مع اللوحة العربية)
        root.bind("<Control-Return>", lambda e: (self._start_or_stop(), "break")[1])
        theme.bind_key(root, 79, self._choose_folder)              # Ctrl+O
        theme.bind_key(root, 79, self._add_file, shift=True)       # Ctrl+Shift+O
        theme.bind_key(root, 188, self.open_settings)              # Ctrl+,
        if self.settings.last_folder and Path(self.settings.last_folder).is_dir():
            self._load_folder(Path(self.settings.last_folder))

    # ------------------------------------------------------------ البناء
    def _var(self, name: str, factory):
        v = getattr(self, name, None)
        if v is None:
            v = factory()
            setattr(self, name, v)
        return v

    def _build(self) -> None:
        t = theme.tokens()
        root = self.root
        root.configure(background=t["bg"])
        body = self.body = tk.Frame(root, background=t["bg"], borderwidth=0, highlightthickness=0)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(3, weight=1)
        self.status_var = self._var("status_var", tk.StringVar)
        self.mode_var = self._var("mode_var", lambda: tk.StringVar(value=self.settings.engine_mode or "local"))
        self.all_var = self._var("all_var", lambda: tk.BooleanVar(value=False))
        pad = px(S4 + S2)

        # 0) الرأس: الشعار والاسم والإصدار (يمين) — حالة العمل (وسط)
        head = tk.Frame(body, background=t["card"], borderwidth=0, highlightthickness=0, height=px(58))
        head.grid(row=0, column=0, sticky="ew")
        head.pack_propagate(False)
        ui.Btn(head, "", self.open_settings, icon=ui.ICON["settings"], min_width=38).pack(
            side="right", padx=(0, pad))  # الإعدادات (Ctrl+,)
        logo = ctk.CTkFrame(head, width=38, height=38, corner_radius=11, fg_color=t["accent"])
        logo.pack(side="left", padx=(pad, 0), pady=px(10))
        tk.Label(logo, text=ui.ICON["layers"], font=ui._tkfont(20, icons=True), foreground=t["on_accent"],
                 background=t["accent"], borderwidth=0).place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(head, text=f"pdf2md", font=ui._tkfont(22, "bold"), foreground=t["fg"], background=t["card"],
                 borderwidth=0).pack(side="left", padx=px(S3))
        tk.Label(head, text=f"v{__version__}", font=("Consolas", -px(12)), foreground=t["muted"],
                 background=t["raised"], borderwidth=0, padx=px(8), pady=px(2)).pack(side="left")
        mid = tk.Frame(head, background=t["card"])
        mid.place(relx=0.5, rely=0.5, anchor="center")
        self.head_dot = tk.Canvas(mid, width=px(9), height=px(9), background=t["card"], highlightthickness=0)
        self.head_dot.pack(side="right", padx=(px(6), 0))
        self.head_lbl = tk.Label(mid, font=ui._tkfont(13), background=t["card"], borderwidth=0)
        self.head_lbl.pack(side="right")
        tk.Frame(body, background=t["line"], height=1).grid(row=0, column=0, sticky="sew")

        # 1) الشريط الأول: الإعدادات والسجل (يمين) — اختيار المجلد والمسار (يسار)
        bar1 = tk.Frame(body, background=t["bg"], borderwidth=0, highlightthickness=0)
        bar1.grid(row=1, column=0, sticky="ew", padx=pad, pady=(px(S3), px(S2)))
        self.log_btn = ui.Btn(bar1, "السجل", self._toggle_details, icon=ui.ICON["history"], kind="ghost")
        self.log_btn.pack(side="right")
        field = ctk.CTkFrame(bar1, height=38, corner_radius=8, border_width=1, fg_color=t["card"],
                             border_color=t["line"])
        field.pack(side="left", fill="x", expand=True, padx=(0, px(S3)))
        field.pack_propagate(False)
        tk.Label(field, text=ui.ICON["folder"], font=ui._tkfont(15, icons=True), foreground=t["accent_text"],
                 background=t["card"], borderwidth=0).pack(side="right", padx=px(S3))
        self.folder_lbl = ui.PathLabel(field, t["card"], t["fg"])
        self.folder_lbl.pack(side="left", fill="x", expand=True, padx=px(S3))
        if self.settings.last_folder:
            self.folder_lbl.set(self.settings.last_folder)
        else:
            self.folder_lbl.set(R("اضغط هنا لاختيار مجلد"))

        def clickable(w):  # الحقل كله (والأيقونة) يفتح اختيار المجلد
            try:
                w.configure(cursor="hand2")
            except tk.TclError:
                pass
            w.bind("<Button-1>", lambda e: self._choose_folder(), add="+")
            for c in w.winfo_children():
                clickable(c)
        clickable(field)

        # 2) شريط القائمة: تحديد الكل والعدد (يمين) — إضافة ملف وحذف (يسار)
        bar2 = tk.Frame(body, background=t["bg"], borderwidth=0, highlightthickness=0)
        bar2.grid(row=2, column=0, sticky="ew", padx=pad, pady=(px(S2), px(S2)))
        self.all_chk = ui.CheckRow(bar2, "تحديد الكل", self.all_var, lambda: self._set_checked(self.all_var.get()),
                                   t["bg"])
        self.all_chk.pack(side="right")
        self.count_lbl = tk.Label(bar2, font=ui._tkfont(12), foreground=t["muted"], background=t["raised"],
                                  borderwidth=0, padx=px(8), pady=px(2))
        ui.Btn(bar2, "", self._remove_selected_extras, icon=ui.ICON["trash"], kind="ghost", min_width=38).pack(
            side="left")

        # 3) جدول الملفات
        self.table = ui.FileTable(body, on_toggle=lambda k: self._toggle_keys([k]), on_menu=self._file_menu,
                                  on_remove=self._remove_keys, on_add=self._add_file)
        self.table.grid(row=3, column=0, sticky="nsew", padx=pad, pady=(0, px(S3)))
        self.tree = self.table  # اسم قديم تستخدمه بقية الدوال

        # 4) بعد الانتهاء
        done = self.done_frame = tk.Frame(body, background=t["bg"], borderwidth=0, highlightthickness=0)
        self.done_label = tk.Label(done, text=R("تم"), font=ui._tkfont(16, "bold"), foreground=t["ok"],
                                   background=t["bg"], borderwidth=0)
        self.done_label.pack(side="right", padx=(0, px(S3)))
        # الخطوة التالية الأهم: مراجعة الصفحات الضعيفة (زر رئيسي)، ثم المجلد والفهرس (ثانويان)
        self.review_btn = ui.Btn(done, "", self._review_clicked, kind="primary")
        self.folder_btn = ui.Btn(done, "فتح المجلد", self._open_output)
        self.index_btn = ui.Btn(done, "فتح الفهرس", lambda: self._open_output_file("INDEX.md"))
        self.log_text = theme.text_widget(body, height=8)
        self._details_shown = False

        # 6) الشريط السفلي: نمط المعالجة، التقدّم، الحالة، ابدأ
        bottom = tk.Frame(body, background=t["card"], borderwidth=0, highlightthickness=0)
        bottom.grid(row=6, column=0, sticky="ew")
        tk.Frame(bottom, background=t["line"], height=1).pack(fill="x")
        inner = tk.Frame(bottom, background=t["card"], borderwidth=0, highlightthickness=0)
        inner.pack(fill="x", padx=pad, pady=(px(S2), px(S3)))
        row_a = tk.Frame(inner, background=t["card"])
        row_a.pack(fill="x")
        tk.Label(row_a, text=R("نمط المعالجة:"), font=ui._tkfont(15, "bold"), foreground=t["accent_text"],
                 background=t["card"],
                 borderwidth=0).pack(side="right", padx=(0, px(S3)))
        self.seg = ui.Segmented(row_a, [("auto", "تلقائي", ""), ("local", "داخلي (بدون إنترنت)", ""),
                                        ("api", "عبر المواقع (AI Cloud)", "")],
                                self.mode_var, self._on_mode)
        self.seg.pack(side="right")
        self.mode_buttons = self.seg.buttons
        self.mode_hint = tk.Label(row_a, text=R("أضف مفتاحاً من الإعدادات"), font=ui._tkfont(13, "normal"),
                                  foreground=t["accent_text"], background=t["card"], cursor="hand2", borderwidth=0)
        self.mode_hint.bind("<Button-1>", lambda e: self.open_settings("providers"))
        prog = tk.Frame(inner, background=t["card"])
        prog.pack(fill="x", pady=(px(S2), 0))
        self.bar = ui.ProgressBar(prog, maximum=1000)
        self.bar.pack(fill="x")
        tk.Label(inner, textvariable=self.status_var, anchor="e", font=ui._tkfont(12), foreground=t["muted"],
                 background=t["card"], borderwidth=0).pack(fill="x", pady=(px(S1), 0))
        tk.Frame(inner, background=t["line"], height=1).pack(fill="x", pady=(px(S1), px(S2)))
        row_b = tk.Frame(inner, background=t["card"])
        row_b.pack(fill="x")
        # يسار: ابدأ وإلغاء والإيقاف المؤقت (تُحزم أولاً فلا تُضغط أبداً) — يمين: حالة المحرّك والصيغة،
        # وتختفي "الصيغة المستهدفة" ثم يُختصر نص المحرّك إذا ضاقت النافذة
        self.start_btn = ui.Btn(row_b, "ابدأ", self._start_or_stop, icon=ui.ICON["arrow"], kind="primary", height=48,
                                radius=12, icon_end=True, min_width=210, size=18)
        self.start_btn.pack(side="left")
        self.cancel_btn = ui.Btn(row_b, "إلغاء الأمر", self._start_or_stop, kind="ghost", height=38, radius=10,
                                 min_width=100)
        self.cancel_btn.pack(side="left", padx=(px(S3), 0))
        self.cancel_btn.configure(state="disabled")
        self.pause_btn = ui.Btn(row_b, "إيقاف مؤقت", self._toggle_pause, kind="ghost", height=38, radius=10,
                                min_width=110)
        self.pause_btn.pack(side="left", padx=(px(S2), 0))
        self.pause_btn.configure(state="disabled")
        info = tk.Frame(row_b, background=t["card"])
        info.pack(side="right", fill="x", expand=True, padx=(px(S3), 0))
        self.engine_icon = tk.Label(info, font=ui._tkfont(14, icons=True), background=t["card"], borderwidth=0)
        self.engine_icon.pack(side="right")
        self.engine_lbl = tk.Label(info, font=ui._tkfont(13), background=t["card"], borderwidth=0, anchor="e")
        self.engine_lbl.pack(side="right", padx=(0, px(8)))
        self.engine_link = tk.Label(info, text=R("تثبيته"), font=ui._tkfont(13), foreground=t["accent_text"],
                                    background=t["card"], cursor="hand2", borderwidth=0)
        self.engine_link.bind("<Button-1>", lambda e: self.open_settings("local"))
        self._fmt_sep = tk.Label(info, text="|", font=ui._tkfont(13), foreground=t["line"], background=t["card"],
                                 borderwidth=0)
        self._fmt_lbl = tk.Label(info, text=R("الصيغة المستهدفة:") + "  Markdown (.md)", font=ui._tkfont(13),
                                 foreground=t["muted"], background=t["card"], borderwidth=0)
        self._fmt_sep.pack(side="right", padx=px(S3))
        self._fmt_lbl.pack(side="right")
        info.bind("<Configure>", lambda e: self._fit_info(info, e.width))
        self._set_head("جاهز للعمل", "ok")
        self._refresh_mode()

    def _fit_info(self, info: tk.Frame, width: int) -> None:
        """حسب عرض النافذة: تُخفى "الصيغة المستهدفة" إذا لم يتسع السطر لحالة المحرّك معها."""
        texts = getattr(self, "_engine_texts", None)
        if texts and width > 1:   # نص المحرّك الكامل إن اتسع، وإلا المختصر
            self.engine_lbl.configure(text=texts[0])
            if self.engine_lbl.winfo_reqwidth() + self.engine_icon.winfo_reqwidth() + px(16) > width:
                self.engine_lbl.configure(text=texts[1])
        need = sum(w.winfo_reqwidth() for w in (self.engine_icon, self.engine_lbl)) + px(16)
        fmt = self._fmt_lbl.winfo_reqwidth() + self._fmt_sep.winfo_reqwidth() + px(S3 * 2)
        shown = self._fmt_lbl.winfo_ismapped()
        if need + fmt > width and shown:
            self._fmt_sep.pack_forget()
            self._fmt_lbl.pack_forget()
        elif need + fmt <= width - px(8) and not shown:
            self._fmt_sep.pack(side="right", padx=px(S3))
            self._fmt_lbl.pack(side="right")

    def _fit_min_height(self) -> None:
        """أقل ارتفاع يُظهر كل الصفوف الثابتة (الرأس، الأشرطة، السفلي) مع جدول صغير، ضمن منطقة العمل."""
        self.root.update_idletasks()
        fixed = self.body.winfo_reqheight() - self.table.winfo_reqheight()
        _x, _y, _w, ah = theme.work_area(self.root)
        need = min(fixed + px(190) + px(48), ah - px(8))
        cur_w = self.root.winfo_width()
        self.root.minsize(self.root.minsize()[0], need)
        if self.root.winfo_height() < need:
            self.root.geometry(f"{cur_w}x{need}")

    def _set_head(self, text: str, tone: str) -> None:
        t = theme.tokens()
        col = {"ok": t["ok"], "busy": t["accent_text"]}.get(tone, t["muted"])
        self.head_lbl.configure(text=R(text), foreground=t["muted"])
        self.head_dot.delete("all")
        self.head_dot.create_oval(1, 1, px(8), px(8), fill=col, outline=col)

    def rebuild_ui(self) -> None:
        """يعيد بناء النافذة الرئيسية بألوان الثيم الحالي مع الإبقاء على الملفات والحالة."""
        log_text = self.log_text.get("1.0", "end") if hasattr(self, "log_text") else ""
        value = self.bar["value"] if hasattr(self, "bar") else 0
        done = self.done_frame.winfo_ismapped() if hasattr(self, "done_frame") else False
        for w in self.root.winfo_children():
            if not isinstance(w, tk.Toplevel):
                w.destroy()
        self._build()
        for k in self.files:
            self.table.insert("", "end", iid=k)
            self._row(k)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", log_text.rstrip("\n") + "\n" if log_text.strip() else "")
        self.log_text.configure(state="disabled")
        self.bar.configure(value=value)
        self._update_count()
        self._set_running(self.busy())
        if done:
            self._show_done(True)

    # ------------------------------------------------------------ الوضع
    def _gate_mode(self) -> str:
        """"تلقائي" يفتح البوابة فقط إذا وُجد مزوّد مفعّل بمفتاح؛ وإلا يبقى داخلياً بالكامل."""
        if self.settings.engine_mode == "api":
            return CLOUD
        if self.settings.engine_mode == "auto" and self.provider_config.active(self.secrets):
            return CLOUD
        return LOCAL

    def _set_mode(self, mode: str) -> None:
        self.mode_var.set(mode)
        self.settings.engine_mode = mode
        self.settings.use_local_engine = mode == "local"
        self.settings.api_for_scanned = mode in ("api", "auto")
        self.settings.save()
        self.gate.set_mode(self._gate_mode())

    def _on_mode(self) -> None:
        mode = self.mode_var.get()
        cloud = mode == "api" or (mode == "auto" and self.provider_config.active(self.secrets))
        if cloud and not self.settings.cloud_notice_dismissed:
            ok, dismiss = self._cloud_notice()
            if not ok:
                self.mode_var.set(self.settings.engine_mode or "local")
                return
            if dismiss:
                self.settings.cloud_notice_dismissed = True
        if self.busy():
            # أثناء الإيقاف المؤقت: تغيير النمط يوقف الدفعة ثم يعيد تشغيلها بالنمط الجديد؛
            # الصفحات المنجزة محفوظة في نقطة الحفظ فيُكمل من حيث توقف
            if mode == (self.settings.engine_mode or "local"):
                self._switch_mode = None
                return
            self._switch_mode = mode
            for b in self.mode_buttons.values():
                b.configure(state="disabled")
            self.pause_btn.configure(state="disabled")
            self.pause_event.clear()
            self.stop_event.set()
            self.status_var.set(R("جارٍ التحويل إلى النمط الجديد بعد الصفحة الحالية…"))
            self.log("⇄ تحويل النمط — يُستكمل بالنمط الجديد بعد انتهاء الصفحة الجارية")
            return
        self._set_mode(mode)
        self._refresh_privacy()

    def _cloud_notice(self) -> tuple[bool, bool]:
        win = tk.Toplevel(self.root)
        win.title(R("عبر المواقع"))
        win.transient(self.root)
        win.resizable(False, False)
        theme.dark_titlebar(win)
        f = ttk.Frame(win, padding=px(S4 + S2))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=R("الملفات ستُرسل إلى المزوّدات المفعّلة خارج جهازك."), anchor="e",
                  justify="right").pack(fill="x")
        again = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text=theme.ck("لا تعرض هذا مرة أخرى"), variable=again).pack(anchor="e", pady=(px(S3), 0))
        res = {"ok": False}
        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(px(S4), 0))

        def close(ok):
            res["ok"] = ok
            win.destroy()

        ttk.Button(btns, text=R("موافق"), style="Accent.TButton", width=10, command=lambda: close(True)).pack(side="right")
        ttk.Button(btns, text=R("إلغاء"), width=10, command=lambda: close(False)).pack(side="right", padx=px(S2))
        win.protocol("WM_DELETE_WINDOW", lambda: close(False))
        win.bind("<Escape>", lambda e: close(False))
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        self.root.wait_window(win)
        return res["ok"], bool(again.get())

    def _refresh_mode(self) -> None:
        """"عبر المواقع" لا يُفعَّل إلا بمزوّد مفعّل له مفتاح محفوظ."""
        active = self.provider_config.active(self.secrets)
        self.mode_buttons["api"].configure(state="normal" if active else "disabled")
        if not active and self.mode_var.get() == "api":
            self._set_mode("local")
        if active:
            self.mode_hint.pack_forget()
        else:
            self.mode_hint.pack(side="right", padx=px(S3))
        self.gate.set_mode(self._gate_mode())
        self._refresh_privacy(active)

    def _refresh_privacy(self, active=None) -> None:
        """حالة المحرّك أسفل النافذة وعنوان زر ابدأ. لا تنبيه ملوّن: اختيار الوضع نفسه واضح."""
        t = theme.tokens()
        running = self.busy()
        self.engine_link.pack_forget()
        if self.mode_var.get() == "auto":
            tiers = self._auto_tiers()
            # النص الكامل إن اتسع السطر، وإلا المختصر (_fit_info يقرر حسب عرض النافذة، مثل "عبر المواقع")
            self._engine_texts = ((R("تلقائي: " + " ← ".join(tiers)), R("تلقائي (" + {1: "درجة واحدة", 2: "درجتان"}.get(len(tiers), f"{len(tiers)} درجات") + ")"))
                                  if tiers else (R("تلقائي: لا يوجد محرّك (النصية فقط)"), R("تلقائي: لا محرّك")))
            self.engine_icon.configure(text=ui.ICON["check"] if tiers else ui.ICON["shield"],
                                       foreground=t["ok"] if tiers else t["muted"])
            self.engine_lbl.configure(text=self._engine_texts[0], foreground=t["ok"] if tiers else t["muted"])
            if not tiers:
                self.engine_link.pack(side="right", padx=px(S2), after=self.engine_lbl)
            self.root.after_idle(lambda: self._fit_info(self.engine_lbl.master, self.engine_lbl.master.winfo_width()))
            if not running:
                self.start_btn.configure(text=R("ابدأ"))
            return
        if self.mode_var.get() == "api":
            if active is None:
                active = self.provider_config.active(self.secrets)
            names = "، ".join(spec.name for spec, _st, _k in active)
            ok = bool(names)
            self.engine_icon.configure(text=ui.ICON["check"] if ok else ui.ICON["cloud"],
                                       foreground=t["ok"] if ok else t["muted"])
            self._engine_texts = ((R(f"المزوّد جاهز: {names}"), R(f"جاهز ({len(active)} مزوّد)")) if ok
                                  else (R("لا يوجد مزوّد مفعّل"),) * 2)
            self.engine_lbl.configure(text=self._engine_texts[0], foreground=t["ok"] if ok else t["muted"])
            self.root.after_idle(lambda: self._fit_info(self.engine_lbl.master, self.engine_lbl.master.winfo_width()))
            if not running:
                self.start_btn.configure(text=R("ابدأ"))
            return
        self._engine_texts = None
        from .. import engine_choice as ec
        ready = ec.effective(self.settings)[0] != ec.NONE
        self.engine_icon.configure(text=ui.ICON["check"] if ready else ui.ICON["shield"],
                                   foreground=t["ok"] if ready else t["muted"])
        self.engine_lbl.configure(text=R("محرّك الاستخراج الداخلي جاهز" if ready else "النموذج الداخلي غير مثبّت"),
                                  foreground=t["ok"] if ready else t["muted"])
        if not ready:
            self.engine_link.pack(side="right", padx=px(S2), after=self.engine_lbl)
        if not running:
            self.start_btn.configure(text=R("ابدأ"))

    def _auto_tiers(self) -> list[str]:
        """درجات "تلقائي" المتاحة الآن، من الأخف للأقوى: داخلي ← OCR ← ذكاء اصطناعي."""
        from .. import engine_choice as ec
        tiers = []
        if ec.effective(self.settings)[0] != ec.NONE:
            tiers.append("داخلي")
        kinds = {spec.kind for spec, _st, _k in self.provider_config.active(self.secrets)}
        if "ocr" in kinds:
            tiers.append("OCR")
        if kinds - {"ocr"}:
            tiers.append("ذكاء اصطناعي")
        return tiers

    def open_settings(self, tab: str = "") -> None:
        from .settings_window import SettingsWindow
        if self.settings_win is not None and self.settings_win.alive():
            self.settings_win.show(tab)
            return
        self.settings_win = SettingsWindow(self, tab)

    def on_settings_changed(self) -> None:
        self._refresh_mode()

    # ------------------------------------------------------------ أدوات
    def log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        for line in text.splitlines() or [""]:
            for part in theme.split_long(line):
                self.log_text.insert("end", R(part) + "\n", "rtl")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def output_dir(self) -> Path | None:
        """مجلد العمل المؤقت (الحالة والتقارير والفهرس) — ليس مكان ملفات الـ Markdown."""
        return work_dir()

    def md_folder(self) -> Path | None:
        """أين تُكتب ملفات الـ Markdown: المجلد المختار في الإعدادات، وإلا بجانب ملفات PDF."""
        if self.settings.output_dir:
            return Path(self.settings.output_dir)
        return Path(self.settings.last_folder) if self.settings.last_folder else None

    def _toggle_details(self) -> None:
        self._details_shown = not self._details_shown
        if self._details_shown:
            self.log_text.grid(row=5, column=0, sticky="nsew", padx=px(S4 + S2), pady=(0, px(S3)))
        else:
            self.log_text.grid_forget()

    def _show_done(self, show: bool, stopped: bool = False) -> None:
        if not show:
            self.done_frame.grid_forget()
            return
        t = theme.tokens()
        self.done_label.configure(text=R("توقف" if stopped else "تم"), foreground=t["warn"] if stopped else t["ok"])
        out = self.output_dir()
        n = self._pending_review_total()
        self._refresh_review_counts()
        for b in (self.review_btn, self.folder_btn, self.index_btn):
            b.pack_forget()
        if n:
            self.review_btn.configure(text=R(f"راجع {n} صفحات" if n != 1 else "راجع صفحة واحدة"))
            self.review_btn.pack(side="right", padx=(0, px(S2)))
        self.folder_btn.pack(side="right", padx=(0, px(S2)))
        if out is not None and (out / "INDEX.md").is_file():
            self.index_btn.pack(side="right", padx=(0, px(S2)))
        self.done_frame.grid(row=4, column=0, sticky="ew", padx=px(S4 + S2), pady=(0, px(S3)))

    def _row(self, key: str) -> None:
        e = self.files.get(key)
        if not e or not self.table.exists(key):
            return
        pages = "" if e.pages is None else str(e.pages)
        rng = self.file_options.page_range(e.file_hash) if e.file_hash else ""
        if rng and e.pages:
            pages = f"{rng} / {e.pages}"
        status = e.error or e.status
        if e.file_hash and self.file_options.force_ocr(e.file_hash):
            status = (status + " · OCR").strip(" ·")
        if e.file_hash and self.file_options.chat(e.file_hash):
            status = (status + " · محادثات").strip(" ·")
        tone = "err" if e.error else ("ok" if e.status == "جاهز" else ("busy" if e.status == "جارٍ الفحص" else ""))
        parts = [f"{e.size / 1048576:.1f} ميغابايت" if e.size else "", "مضاف يدوياً" if e.extra else ""]
        # بلا الامتداد: ".pdf" بعد رقم داخل نص عربي ينقلب ("pdf.2024")
        self.table.set_row(key, e.path.stem, pages, status or "—", " • ".join(x for x in parts if x),
                           key in self.checked, tone)

    def _pending_review_by_file(self) -> dict[str, int]:
        """الصفحات المعلّقة فعلاً للملفات الموجودة في الجدول، من حالة الصفحات (لا من التقرير النصي القديم)."""
        out = self.output_dir()
        if out is None:
            return {}
        from ..review_report import pending_by_hash
        return pending_by_hash(out, [e.file_hash for e in self.files.values() if e.file_hash])

    def _pending_review_total(self) -> int:
        return sum(self._pending_review_by_file().values())

    def _refresh_review_counts(self) -> None:
        """عمود الحالة «تم · N للمراجعة» يتبع الواقع بعد المراجعة (كان يبقى بالرقم القديم)."""
        pending = self._pending_review_by_file()
        for key, e in self.files.items():
            if e.file_hash and e.status.startswith("تم"):
                n = pending.get(e.file_hash, 0)
                e.status = "تم" + (f" · {n} للمراجعة" if n else "")
                self._row(key)

    def _offer_pending_review(self, total: int) -> None:
        """بعد إعادة تشغيل البرنامج: إن كانت في مجلد الإخراج صفحات معلّقة يظهر زر «راجع N صفحات»."""
        try:
            if total and not self.busy() and not self.done_frame.winfo_ismapped() and self.output_dir() is not None:
                if self._pending_review_total():
                    self._show_done(True)
        except (AttributeError, tk.TclError, OSError):
            pass  # الواجهة لم تكتمل بعد

    def _update_count(self) -> None:
        n, total = len(self.checked), len(self.files)
        self.count_lbl.configure(text=R(f"{n} من {total}") if total else "")
        if total:
            self.count_lbl.pack(side="right", padx=px(S3))
        else:
            self.count_lbl.pack_forget()
        self.all_var.set(bool(total) and n == total)
        self.all_chk.refresh()
        self._offer_pending_review(total)
        if total:
            self.table.set_zone_text("أضف ملفات PDF أخرى إلى القائمة", "اضغط هنا لاختيار ملفات متعددة دفعة واحدة")
        else:
            self.table.set_zone_text("اختر مجلداً فيه ملفات PDF  (Ctrl+O)",
                                     "أو اضغط هنا لإضافة ملفات PDF إلى قائمة التحويل")

    # ------------------------------------------------------------ الملفات
    def _choose_folder(self) -> None:
        if self.busy():
            return
        d = filedialog.askdirectory(title="اختر مجلد ملفات PDF", initialdir=self.settings.last_folder or None)
        if d:
            self.settings.extra_files = []
            self._load_folder(Path(d))

    def _add_file(self) -> None:
        if self.busy():
            return
        paths = filedialog.askopenfilenames(title="إضافة ملفات PDF أو صور",
                                               filetypes=[("PDF وصور", "*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp"),
                                                          ("PDF", "*.pdf"), ("صور", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp")])
        new = [Path(p) for p in paths if str(Path(p)) not in self.files]
        if not new:
            return
        for p in new:
            self.settings.extra_files.append(str(p))
            self._insert(p, extra=True)
            self.checked.add(str(p))
            self._row(str(p))
        if not self.settings.last_folder:
            self.settings.last_folder = str(new[0].parent)
            self.folder_lbl.set(str(new[0].parent))
        self.settings.save()
        self._update_count()
        threading.Thread(target=self._scan_worker, args=(self.scan_token, new), daemon=True).start()

    def _list_pdfs(self, folder: Path) -> list[Path]:
        if self.settings.include_subfolders:
            out_dir = self.output_dir()
            it = (p for p in folder.rglob("*") if p.suffix.lower() in INPUT_EXTS
                  and not (out_dir and out_dir in p.parents) and OUTPUT_NAME not in p.parts)
        else:
            it = (p for p in folder.iterdir() if p.suffix.lower() in INPUT_EXTS)
        return sorted((p for p in it if p.is_file()), key=lambda p: str(p.relative_to(folder)).lower())

    def _insert(self, p: Path, extra: bool = False) -> None:
        key = str(p)
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        self.files[key] = FileEntry(p, status="جارٍ الفحص", extra=extra, size=size)
        self.table.insert("", "end", iid=key)
        self._row(key)

    def _load_folder(self, folder: Path) -> None:
        self.settings.last_folder = str(folder)
        self.settings.save()
        self.folder_lbl.set(str(folder))
        self.tree.delete(*self.tree.get_children())
        self.files.clear()
        self.checked.clear()
        self._show_done(False)
        try:
            pdfs = self._list_pdfs(folder)
        except OSError as e:
            log.warning("تعذّرت قراءة المجلد %s: %s", folder, e)
            messagebox.showerror("تعذّرت قراءة المجلد", theme.friendly_error(e))
            return
        extras = [Path(x) for x in self.settings.extra_files if Path(x).is_file() and Path(x) not in pdfs]
        for p in pdfs:
            self._insert(p)
        for p in extras:
            self._insert(p, extra=True)
        self.checked = set(self.files)
        for k in self.files:
            self._row(k)
        self._update_count()
        self.log(f"{len(pdfs) + len(extras)} ملف PDF في {folder}")
        self.scan_token += 1
        threading.Thread(target=self._scan_worker, args=(self.scan_token, pdfs + extras), daemon=True).start()

    def _scan_worker(self, token: int, pdfs: list[Path]) -> None:
        """يحسب عدد الصفحات والبصمة لكل ملف في الخلفية."""
        for p in pdfs:
            if token != self.scan_token:
                return
            pages, file_hash, err = None, None, ""
            try:
                with fitz.open(p) as doc:
                    pages = doc.page_count
                    if doc.needs_pass:
                        err = "محمي بكلمة مرور"
                file_hash = self.hash_cache.get(p)
            except Exception as e:
                err = "تعذّر فتحه"
                log.warning("تعذّر فتح %s: %s", p, e)
            resume = None
            if file_hash and not err:
                from .. import checkpoint as cp
                resume = cp.progress(self.output_dir() / ".state", file_hash)
                if resume and resume[0] >= resume[1]:
                    resume = None   # مكتمل: لا يحتاج استكمالاً
            self.events.put(("scan", {"token": token, "key": str(p), "pages": pages, "hash": file_hash, "error": err,
                                      "resume": resume}))
        self.hash_cache.save()

    def rescan(self) -> None:
        """يضيف الملفات الجديدة ويحذف المحذوفة بدون مسح القائمة أو التحديد."""
        folder = Path(self.settings.last_folder) if self.settings.last_folder else None
        if not folder or not folder.is_dir():
            return
        try:
            current = {str(p): p for p in self._list_pdfs(folder)}
        except OSError:
            return
        added = [p for k, p in current.items() if k not in self.files]
        removed = [k for k, e in self.files.items() if k not in current and not (e.extra and e.path.is_file())]
        if self.busy():
            removed = []  # لا نحذف ملفاً قيد المعالجة
        for k in removed:
            self.files.pop(k, None)
            self.checked.discard(k)
            if self.tree.exists(k):
                self.tree.delete(k)
        for p in added:
            self._insert(p)
        if added:
            self.log(f"أُضيف {len(added)} ملف جديد")
            threading.Thread(target=self._scan_worker, args=(self.scan_token, added), daemon=True).start()
        if added or removed:
            self._update_count()

    def _watch_folder(self) -> None:
        try:
            self.rescan()
        finally:
            self.root.after(3000, self._watch_folder)

    def _toggle_keys(self, keys) -> None:
        for k in keys:
            if k in self.checked:
                self.checked.discard(k)
            elif k in self.files:
                self.checked.add(k)
            self._row(k)
        self._update_count()

    def _set_checked(self, value: bool) -> None:
        self.checked = set(self.files) if value else set()
        for k in self.files:
            self._row(k)
        self._update_count()

    def _file_menu(self, key: str, x_root: int, y_root: int) -> None:
        keys = [k for k in (self.table.selection() or (key,)) if k in self.files]
        entries = [self.files[k] for k in keys]
        m = ui.PopupMenu(self.root)
        hashes = [e.file_hash for e in entries]
        if all(hashes):
            forced = all(self.file_options.force_ocr(h) for h in hashes)
            m.add_command(label=R("إلغاء فرض OCR" if forced else "فرض OCR (تجاهل طبقة النص)"),
                          command=lambda: self._set_force_ocr(entries, not forced))
        if len(entries) == 1 and entries[0].file_hash:
            m.add_command(label=R("تحديد الصفحات…"), command=lambda: self._ask_page_range(entries[0]))
            e0 = entries[0]
            is_chat = self.file_options.chat(e0.file_hash)
            m.add_command(label=R("إلغاء: لقطات محادثات" if is_chat else "لقطات محادثات (تمييز المتحدثين)"),
                          command=lambda: (self.file_options.set_chat(e0.file_hash, e0.path.name, not is_chat),
                                           self._row(str(e0.path))))
        if len(entries) == 1 and not self.busy():
            m.add_command(label=R("تصدير إلى Word (.docx)"), command=lambda: self._export_word(entries[0]))
        if all(hashes) and not self.busy():
            m.add_command(label=R("إعادة المعالجة من الصفر"), command=lambda: self._reset_files(entries))
        if len(entries) == 1:
            m.add_command(label=R("فتح الملف"), command=lambda: os.startfile(entries[0].path))  # noqa: S606
        extras = [e for e in entries if e.extra]
        if extras:
            m.add_command(label=R("إزالة من القائمة"), command=lambda: self._remove_extra(extras))
        if m.index("end") is not None:
            m.tk_popup(x_root, y_root)

    def _export_word(self, e) -> None:
        """يحوّل ملف الـ Markdown الناتج لهذا الـ PDF إلى Word (عربي ⇒ يمين لليسار)."""
        from ..docx_out import export_markdown_file
        folder = self.md_folder() if self.settings.output_dir else e.path.parent
        md = Path(folder) / (e.path.stem + ".md") if folder else None
        if not md or not md.is_file():
            messagebox.showinfo("لا يوجد Markdown", "حوّل الملف أولاً (زر «ابدأ») ثم صدّره إلى Word.", parent=self.root)
            return
        try:
            out = export_markdown_file(md)
        except Exception as ex:
            log.exception("تعذّر إنشاء ملف Word")
            messagebox.showerror("تعذّر إنشاء ملف Word", theme.friendly_error(ex), parent=self.root)
            return
        self.log(f"✔ أُنشئ ملف Word: {out}")
        os.startfile(out)  # noqa: S606

    def _reset_files(self, entries: list) -> None:
        """البرنامج يحفظ ما قرأه من كل صفحة ولا يعيد إرسالها؛ هذا يمسح ذلك ليُقرأ الملف من جديد."""
        from .. import checkpoint as cp
        names = "، ".join(e.path.stem for e in entries[:3]) + ("…" if len(entries) > 3 else "")
        pages = sum(e.pages or 0 for e in entries)
        if not messagebox.askyesno(
                "إعادة المعالجة من الصفر",
                f"{names}\n\nسيُنسى كل ما قُرئ سابقاً من {'هذا الملف' if len(entries) == 1 else 'هذه الملفات'} "
                f"وتُعاد قراءة الصفحات عند الضغط على «ابدأ».\nفي وضع «عبر المواقع» يستهلك ذلك حتى {pages} طلباً "
                "من الحصة.\n\nملف الـ PDF وملف الـ Markdown لا يُحذفان.\nمتابعة؟", parent=self.root):
            return
        state_dir = self.output_dir() / ".state"
        for e in entries:
            cp.reset_file(state_dir, e.file_hash)
            e.status = "جاهز"
            self._row(str(e.path))
        self.log(f"↺ أُعيد ضبط {len(entries)} ملف — اضغط «ابدأ» لقراءته من جديد")

    def _ask_page_range(self, e) -> None:
        from ..file_options import parse_page_range
        cur = self.file_options.page_range(e.file_hash)
        spec = simpledialog.askstring(
            "تحديد الصفحات", f"{e.path.stem} — {e.pages or '?'} صفحة\nالصفحات المطلوب قراءتها، مثال: 1-5 أو 1,3,7-9\n"
            "اتركه فارغاً لقراءة كل الصفحات:", initialvalue=cur, parent=self.root)
        if spec is None:
            return
        try:
            parse_page_range(spec, e.pages or 10 ** 6)
        except ValueError as ex:
            messagebox.showerror("تحديد الصفحات", f"صيغة غير صحيحة: {ex}")
            return
        self.file_options.set_page_range(e.file_hash, e.path.name, spec)
        self._row(str(e.path))

    def _remove_keys(self, keys) -> None:
        self._remove_extra([self.files[k] for k in keys if k in self.files and self.files[k].extra])

    def _remove_selected_extras(self) -> None:
        self._remove_keys(self.table.selection())

    def _set_force_ocr(self, entries: list[FileEntry], value: bool) -> None:
        self.file_options.set_force_ocr([(e.file_hash, e.path.name) for e in entries], value)
        for e in entries:
            self._row(str(e.path))

    def _remove_extra(self, entries: list[FileEntry]) -> None:
        for e in entries:
            k = str(e.path)
            self.files.pop(k, None)
            self.checked.discard(k)
            if self.tree.exists(k):
                self.tree.delete(k)
            if k in self.settings.extra_files:
                self.settings.extra_files.remove(k)
        self.settings.save()
        self._update_count()

    # ------------------------------------------------------------ الإخراج
    def _open_output(self) -> None:
        out = self.md_folder()
        if out and out.is_dir():
            os.startfile(out)  # noqa: S606 — يفتح المجلد في مستكشف ويندوز
        else:
            messagebox.showinfo("لا يوجد مجلد إخراج", "لم يُنشأ مجلد الإخراج بعد. اضغط «ابدأ» أولاً.")

    def _open_output_file(self, name: str) -> None:
        out = self.output_dir()
        target = out / name if out else None
        if target and target.is_file():
            os.startfile(target)  # noqa: S606
        else:
            messagebox.showinfo("الملف غير موجود", f"لم يُنشأ {name} بعد. يظهر بعد معالجة الملفات.")

    def build_index(self, full: bool) -> bool:
        if self.busy():
            return False
        out = self.output_dir()
        if out is None or not (out / ".state").is_dir():
            messagebox.showinfo("الفهرس", "لا توجد ملفات معالجة بعد.")
            return False
        self.stop_event = threading.Event()
        ctx = self._ctx(out)

        def work():
            try:
                if self.settings.summary_via_api and self.gate.mode == CLOUD:
                    ctx.api = self._make_api_runner(ctx)
                build_index_safe(ctx, full=full)
            finally:
                if ctx.api is not None:
                    for s in ctx.api.slots:
                        s.provider.close()
                ctx.emit("finished", stopped=False, index_only=True)

        self._set_running(True)
        self.status_var.set(R("بناء الفهرس…"))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        return True

    # ------------------------------------------------------------ التشغيل
    def _pages_for(self, e) -> list[int] | None:
        from ..file_options import parse_page_range
        try:
            return parse_page_range(self.file_options.page_range(e.file_hash), e.pages or 0)
        except ValueError:
            return None

    def _ctx(self, out: Path) -> BatchContext:
        md = Path(self.settings.output_dir) if self.settings.output_dir else None
        return BatchContext(output_dir=out, settings=self.settings, stop_event=self.stop_event,
                            emit=lambda kind, **kw: self.events.put((kind, kw)), hash_cache=self.hash_cache,
                            beside_source=md is None, md_dir=md, pause_event=self.pause_event)

    def _make_api_runner(self, ctx: BatchContext):
        from ..dispatcher import ApiRunner, build_slots
        slots = build_slots(self.provider_config, self.secrets, self.usage, ctx.stop_event, ctx.emit)
        if not slots:
            return None
        return ApiRunner(self.gate, slots, self.settings, ctx.state_dir, ctx.docs_dir, ctx.emit, ctx.stop_event,
                         md_dir_for=ctx.md_dir_for, pause=ctx.pause_event)

    def selected_spec(self) -> cat.ModelSpec | None:
        ids = [m.id for m in cat.MODELS]
        return cat.MODELS[ids.index(self.settings.local_model)] if self.settings.local_model in ids else None

    def _toggle_pause(self) -> None:
        if not self.busy():
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.configure(text=R("إيقاف مؤقت"))
            for b in self.mode_buttons.values():
                b.configure(state="disabled")
            self._set_head("جارٍ المعالجة", "busy")
            self.log("▶ استكمال")
        else:
            self.pause_event.set()
            self.pause_btn.configure(text=R("استكمال"))
            self._set_head("متوقف مؤقتاً — تكتمل الطلبات الجارية ثم ينتظر", "")
            self.log("⏸ إيقاف مؤقت — يمكنك اختيار نمط آخر (داخلي/عبر المواقع) للاستكمال به")
            for b in self.mode_buttons.values():
                b.configure(state="normal")
            self._refresh_mode()
        self._progress()

    def _set_running(self, running: bool) -> None:
        self.start_btn.configure(state="disabled" if running else "normal")
        self.cancel_btn.configure(state="normal" if running else "disabled")
        self.pause_event.clear()
        self.pause_btn.configure(state="normal" if running else "disabled", text=R("إيقاف مؤقت"))
        for b in self.mode_buttons.values():
            b.configure(state="disabled" if running else "normal")
        self._set_head("جارٍ المعالجة" if running else "جاهز للعمل", "busy" if running else "ok")
        if not running:
            self._refresh_mode()

    def _start_or_stop(self) -> None:
        if self.busy():
            self._switch_mode = None
            self.pause_event.clear()
            self.stop_event.set()
            self.cancel_btn.configure(state="disabled")
            self.status_var.set(R("جارٍ الإيقاف بعد الصفحة الحالية…"))
        else:
            self._start()

    def _start(self) -> None:
        if self.bg.running or (self.settings_win is not None and self.settings_win.alive()
                               and self.settings_win.lab_busy()):
            messagebox.showinfo("ابدأ", "انتظر حتى ينتهي التنزيل أو مختبر النماذج.")
            return
        entries = [self.files[k] for k in self.tree.get_children() if k in self.checked]
        if not entries:
            messagebox.showinfo("ابدأ", "حدد ملفاً واحداً على الأقل." if self.files else "اختر مجلداً أولاً.")
            return
        if any(e.file_hash is None and not e.error for e in entries):
            messagebox.showinfo("ابدأ", "انتظر حتى ينتهي فحص الملفات.")
            return
        jobs = [Job(e.path, e.file_hash, self.settings.force_ocr_all or self.file_options.force_ocr(e.file_hash),
                    pages=self._pages_for(e), chat=self.file_options.chat(e.file_hash))
                for e in entries if e.file_hash and not e.error]
        out = self.output_dir()
        if not jobs or out is None:
            return
        self.settings.api_for_scanned = self.settings.engine_mode in ("api", "auto")
        self.gate.set_mode(self._gate_mode())
        spec = None
        use_paddle = False
        if self.settings.engine_mode == "local":
            from .. import engine_choice as ec
            mode, spec = ec.effective(self.settings)
            use_paddle = mode in (ec.PADDLE, ec.PADDLE_LOCAL)
            if mode == ec.NONE:
                if not messagebox.askyesno(
                        "النموذج الداخلي غير مثبّت", "ستُستخرج الصفحات النصية فقط، وتبقى المصوّرة "
                                 "[غير مقروء].\nلتثبيته: الإعدادات ← داخلي.\n\nمتابعة بدونه؟"):
                    return
                spec = None
        elif self.settings.engine_mode == "auto":
            # تلقائي: النصية من طبقة النص دائماً. المصوّرة: PaddleOCR أولاً (إن ثُبّت) ثم الموجّه يقرر لكل صفحة:
            # تبقى داخلية، أو OCR خارجي للتأكد من الأرقام/إعادة القراءة، أو الذكاء الاصطناعي للجداول والصعب.
            # بلا مزوّد: داخلي بالكامل مع تنبيه بما يُشك فيه. بلا Paddle وبلا مزوّد: النموذج المحلي إن وُجد.
            from .. import engine_choice as ec
            from .. import paddle_engine as pe
            online = bool(self.provider_config.active(self.secrets))
            use_paddle = pe.is_available()
            mode, spec = ec.effective(self.settings)
            if use_paddle or online:
                spec = None  # النموذج المحلي بطيء: لا يُشغَّل إن وُجد Paddle أو مزوّد
            if not use_paddle and not online and spec is None:
                if not messagebox.askyesno(
                        "تلقائي", "لا يوجد محرّك داخلي ولا مزوّد مفعّل: ستُستخرج الصفحات النصية فقط، وتبقى المصوّرة "
                                  "[غير مقروء].\nثبّت PaddleOCR من الإعدادات ← داخلي، أو أضف مفتاحاً.\n\nمتابعة؟"):
                    return
        elif not self.provider_config.active(self.secrets):
            messagebox.showinfo("عبر المواقع", "لا يوجد مزوّد مفعّل بمفتاح.")
            return
        else:
            # "عبر المواقع" مع PaddleOCR مفعّل: Paddle يقرأ الصفحات المصوّرة مجاناً، ولا يُرسل للمواقع إلا
            # المسح الرديء الذي فيه أرقام (أو ما يفشل فيه Paddle)
            from .. import engine_choice as ec
            use_paddle = ec.effective(self.settings)[0] in (ec.PADDLE, ec.PADDLE_LOCAL)
        self.running_jobs = jobs
        self.stop_event = threading.Event()
        ctx = self._ctx(out)
        ctx.provider = None  # يُضبط داخل الخيط بعد تشغيل المحرّك
        self._cur = {"index": 0, "page": 0, "pages": 1}
        total = sum(len(j.pages) if j.pages else (self.files[str(j.path)].pages or 0) for j in jobs)
        self._run = {"t0": time.monotonic(), "done": set(), "total": total, "stage": "", "files": 0}
        self.bar.configure(value=0)
        self._show_done(False)
        self._set_running(True)
        self.status_var.set(R("جارٍ البدء…"))
        self.log(f"بدء معالجة {len(jobs)} ملف — الإخراج في {out}")
        ctx.paddle = True if use_paddle else None  # علامة؛ تُستبدل بالعملية داخل الخيط
        self.worker = threading.Thread(target=self._worker, args=(jobs, ctx, spec), daemon=True)
        self.worker.start()

    def _start_paddle(self, ctx: BatchContext) -> None:
        """يشغّل عملية PaddleOCR المنفصلة. الفشل لا يوقف الدفعة: الصفحات للمحرّك المحلي أو تبقى بانتظاره."""
        from .. import paddle_engine as pe
        ctx.emit("log", text="تشغيل PaddleOCR (عملية منفصلة، بلا إنترنت)…")
        try:
            ctx.paddle = pe.PaddleWorker().start()
            ctx.emit("log", text="PaddleOCR جاهز")
        except Exception as e:
            log.exception("تعذّر تشغيل PaddleOCR")
            ctx.paddle = None
            ctx.emit("log", text=f"✗ تعذّر تشغيل PaddleOCR: {e} — يُكمل بدونه")

    def _worker(self, jobs: list[Job], ctx: BatchContext, spec: cat.ModelSpec | None) -> None:
        if ctx.paddle is True:
            self._start_paddle(ctx)
        if spec is not None:
            from ..providers.local_llama import start_local_provider
            ctx.emit("status", text="تشغيل النموذج الداخلي…")
            ctx.emit("log", text=f"تشغيل المحرّك الداخلي ({spec.label})… قد يستغرق دقيقة.")
            try:
                model_p, mmproj_p = mm.model_paths(spec)
                exe, fallback = mm.engine_for(self.settings.use_gpu)
                ctx.emit("log", text="التشغيل على كرت الشاشة (Vulkan)…" if fallback else "التشغيل على المعالج.")
                self.active_provider = start_local_provider(
                    exe, model_p, mmproj_p, app_data_dir() / "logs", spec.id, stop=ctx.stop_event,
                    prompt=spec.prompt, gpu=bool(fallback), fallback_exe=fallback)
            except Exception as e:
                log.exception("تعذّر تشغيل المحرّك")
                ctx.emit("log", text="✗ تعذّر تشغيل المحرّك الداخلي. " + theme.friendly_error(e).replace("\n", " "))
                if ctx.paddle is not None:
                    ctx.paddle.close()
                    ctx.paddle = None
                ctx.emit("finished", stopped=True)
                return
            ctx.provider = self.active_provider
            ctx.emit("log", text=f"المحرّك جاهز على {self.active_provider.base_url}")
        if self.settings.engine_mode == "api" or (self.settings.engine_mode == "auto"
                                                  and self.provider_config.active(self.secrets)):
            ctx.api = self._make_api_runner(ctx)
        try:
            run_batch(jobs, ctx)
            if self.settings.auto_build_index and not ctx.stop_event.is_set():
                ctx.emit("status", text="بناء الفهرس…")
                build_index_safe(ctx)
        finally:
            if ctx.api is not None:
                for s in ctx.api.slots:
                    s.provider.close()
            if self.active_provider is not None:
                self.active_provider.close()
                self.active_provider = None
            if ctx.paddle is not None:
                ctx.paddle.close()
                ctx.paddle = None

    def _review_clicked(self) -> None:
        """زر «راجع N صفحات»: نافذة المراجعة (معاينة + نص قابل للتعديل). بعد إعادة تشغيل البرنامج تُبنى المهام
        من الملفات المضافة في الجدول؛ وإن لم توجد يُفتح التقرير."""
        out = self.output_dir()
        if out is None or self.busy():
            return
        if not self.running_jobs:
            self.running_jobs = [
                Job(e.path, e.file_hash, False, pages=self._pages_for(e), chat=self.file_options.chat(e.file_hash))
                for e in self.files.values() if e.file_hash and not e.error]
        if self.running_jobs:
            from ..pipeline import review_items
            self.stop_event = threading.Event()
            items = review_items(self.running_jobs, self._ctx(out), require_api=False)
            if items:
                self.pending_review = items
                self._open_review()
                return
        # لا ملفات محمّلة في الجدول: نافذة المراجعة تحتاج الملفات نفسها (التقرير يذكر الأسماء فقط)
        nn = chr(10) * 2
        if messagebox.askyesno("مراجعة الصفحات",
                               "لا توجد ملفات في الجدول لفتح نافذة المراجعة." + nn +
                               "أضف الملفات نفسها (اسحبها أو اختر المجلد) ثم اضغط «راجع» مرة أخرى لتظهر الصور وأزرار القراءة." + nn +
                               "هل تريد فتح التقرير النصي (قائمة بالصفحات المعلّقة فقط، للاطلاع)؟", parent=self.root):
            self._open_output_file("needs_human_review.md")

    def review_engines(self) -> list[tuple[str, str]]:
        """طرق القراءة المتاحة في نافذة المراجعة: (مفتاح، تسمية)."""
        out = [("auto", "تلقائي (Gemini أولاً)")]
        for spec, st, key in self.provider_config.active(self.secrets):
            if spec.id != "gemini":
                out.append((f"slot:{spec.id}", f"{spec.name}" + (" (OCR)" if spec.is_ocr else "")))
        spec = self.selected_spec()
        if spec is not None and mm.is_installed(spec):
            out.append(("local-ai", f"ذكاء محلي: {spec.label}"))
        from .. import paddle_engine as pe
        if pe.is_available():
            out.append(("paddle", "OCR محلي (PaddleOCR — للعربي)"))
        return out

    def _review_read(self, item: dict, engine: str = "auto") -> str:
        """يُستدعى من خيط نافذة المراجعة: قراءة صفحة واحدة بالطريقة التي اختارها المستخدم."""
        if engine == "local-ai":
            return self._review_read_local(item)
        if engine == "paddle":
            return self._review_read_paddle(item)
        from ..pipeline import read_page_now
        # مشغّل جديد لكل قراءة: المحفوظ سابقاً كان يحمل حدث إيقاف قديماً ومزوّداً مغلقاً أو «منتهي الحصة»، فيفشل الزر بعد فترة
        ctx = self._ctx(self.output_dir())
        ctx.stop_event = threading.Event()
        api = self._make_api_runner(ctx)
        if api is None:
            raise RuntimeError("لا يوجد مزوّد مفعّل بمفتاح (أو الوضع داخلي: فعّل «عبر المواقع»)")
        for sl in api.slots:   # القراءة اليدوية لصفحة واحدة: يُسمح بالنماذج القوية الاحتياطية إن حُجبت الخفيفة
            if hasattr(sl.provider, "recitation_models"):
                sl.provider.recitation_models += [m for m in sl.spec.fallback_models if m not in sl.provider.recitation_models]
        try:
            return read_page_now(api, item["path"], item["page"], self.settings,
                                 engine[5:] if engine.startswith("slot:") else "")
        finally:
            for sl in api.slots:
                sl.provider.close()

    def _review_read_local(self, item: dict) -> str:
        """النموذج المحلي الداخلي (llama-server): يُشغَّل عند أول طلب (دقيقة تقريباً) ويبقى حتى إغلاق البرنامج."""
        from ..providers.base import OcrRequest
        from ..providers.local_llama import start_local_provider
        from ..pdf_io import render_page_png
        from ..providers.base import PageImage
        lock = self.__dict__.setdefault("_review_lock", threading.Lock())
        with lock:
            prov = getattr(self, "_review_local", None)
            if prov is None:
                spec = self.selected_spec()
                if spec is None or not mm.is_installed(spec):
                    raise RuntimeError("لا يوجد نموذج داخلي مثبّت (الإعدادات ← داخلي)")
                model_p, mmproj_p = mm.model_paths(spec)
                exe, fallback = mm.engine_for(self.settings.use_gpu)
                prov = start_local_provider(exe, model_p, mmproj_p, app_data_dir() / "logs", spec.id,
                                            prompt=spec.prompt, gpu=bool(fallback), fallback_exe=fallback)
                self._review_local = prov
            with fitz.open(item["path"]) as doc:
                dpi = 150
                img = PageImage(item["page"], render_page_png(doc[item["page"] - 1], dpi), dpi)
            res = prov.ocr(OcrRequest([img]))
        text = (res[0].markdown if res else "").strip()
        if not text:
            raise RuntimeError("النموذج المحلي لم يُخرج نصاً لهذه الصفحة")
        return text

    def _review_read_paddle(self, item: dict) -> str:
        from .. import paddle_engine as pe
        lock = self.__dict__.setdefault("_review_lock", threading.Lock())
        with lock:
            w = getattr(self, "_review_paddle", None)
            if w is None or not w.alive:
                w = self._review_paddle = pe.PaddleWorker().start()
            with fitz.open(item["path"]) as doc:
                png, scale = pe.page_image(doc[item["page"] - 1])
            res = pe.ocr_page(w, png, self.settings.paddle_min_conf, mark_low=False, scale=scale)
        text = (res.markdown or "").strip()
        if not text:
            raise RuntimeError("PaddleOCR لم يُخرج نصاً لهذه الصفحة")
        return text

    def _open_review(self) -> None:
        items, self.pending_review = self.pending_review, []
        if not items:
            return
        from .review_window import ReviewWindow
        ReviewWindow(self.root, items, self._apply_review, reader=self._review_read, engines=self.review_engines())

    def _apply_review(self, decisions: list[dict]) -> None:
        if self.busy():
            messagebox.showinfo("المراجعة", "البرنامج يعمل الآن على دفعة. انتظر حتى تنتهي ثم نفّذ القرارات.", parent=self.root)
            return
        if not self.running_jobs:
            messagebox.showinfo("المراجعة", "لا توجد ملفات محمّلة في الجدول. أضف الملفات من جديد ثم افتح المراجعة.",
                                parent=self.root)
            return
        out = self.output_dir()
        self.stop_event = threading.Event()
        ctx = self._ctx(out)
        jobs = self.running_jobs

        def work():
            to_send = apply_review_decisions(decisions, ctx)
            stopped = False
            if to_send:
                ctx.api = self._make_api_runner(ctx)
                if ctx.api is None:
                    ctx.emit("log", text="لا يوجد مزوّد متاح للإرسال.")
                else:
                    for i in to_send:
                        jobs[i].api_ok = self.gate.allowed(jobs[i].file_hash)
                    stopped = run_api_phase(jobs, ctx, only_index=to_send)
                    for s in ctx.api.slots:
                        s.provider.close()
            ctx.emit("log", text=f"طُبّقت قرارات المراجعة على {len(decisions)} صفحة.")
            ctx.emit("finished", stopped=stopped or self.stop_event.is_set())

        self._set_running(True)
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    # ------------------------------------------------------------ الأحداث
    def _poll_events(self) -> None:
        try:
            while True:
                kind, data = self.events.get_nowait()
                self._handle_event(kind, data)
        except queue.Empty:
            pass
        now = time.monotonic()
        if self.busy() and self._run["t0"] and now - self._run.get("tick", 0) >= 1:   # الوقت يتحرك كل ثانية
            self._run["tick"] = now
            self._progress()
        self.root.after(100, self._poll_events)

    def _progress(self) -> None:
        """الصفحات المنجزة من الإجمالي، الملف الحالي، ما يجري الآن، والوقت المنقضي."""
        n = max(len(self.running_jobs), 1)
        c, r = self._cur, self._run
        total = max(r["total"], 1)
        done = len(r["done"])
        self.bar.configure(value=int(min(done / total, 1.0) * 1000))
        name = self.running_jobs[c["index"]].path.stem if c["index"] < len(self.running_jobs) else ""
        parts = [f"{done} من {r['total']} صفحة", f"الملف {c['index'] + 1} من {n}: {name}"]
        if self.pause_event.is_set():
            parts.append("⏸ متوقف مؤقتاً")
        elif r["stage"]:
            parts.append(r["stage"])
        parts.append(_fmt_elapsed(time.monotonic() - r["t0"]) if r["t0"] else "")
        self.status_var.set(R(" · ".join(x for x in parts if x)))

    def _handle_event(self, kind: str, d: dict) -> None:
        if kind == "scan":
            if d["key"] not in self.files:
                return
            e = self.files[d["key"]]
            e.pages, e.file_hash, e.error = d["pages"], d["hash"], d["error"]
            e.status = "" if e.error else "جاهز"
            if d.get("resume") and not e.error:
                e.status = f"يُستكمل {d['resume'][0]}/{d['resume'][1]}"
            self._row(d["key"])
        elif kind in ("file_start", "page", "file_done", "file_error"):
            if d["index"] >= len(self.running_jobs):
                return
            job = self.running_jobs[d["index"]]
            key = str(job.path)
            e = self.files.get(key)
            if kind == "file_start":
                self._cur = {"index": d["index"], "page": 0, "pages": max(d["pages"], 1)}
                if e:
                    e.status, e.error = "جارٍ", ""
            elif kind == "page":
                self._cur.update(index=d["index"], page=d["page"], pages=max(d["pages"], 1))
                if d.get("status") not in (None, "pending_engine", "todo"):
                    self._run["done"].add((d["index"], d["page"]))
                if e:
                    e.status = f"{d['page']}/{d['pages']}"
            elif kind == "file_done":
                self._cur.update(index=d["index"], page=self._cur["pages"])
                self._run["files"] += 1
                n_rev = d.get("review_count") or 0
                if e:
                    e.status = "تم" + (f" · {n_rev} للمراجعة" if n_rev else "")
                # آخر حالة للملف تغلب (بعد المرحلة الثانية تختفي الصفحات التي أكدها مزوّد خارجي)
                self._run.setdefault("local_warn", {})[d["name"]] = d.get("local_warnings") or {}
                self.log(self._file_summary(d))
            else:
                if e:
                    e.error = "خطأ"
                self.log(f"✗ {d['name']}: {d['error']}")
            self._progress()
            self._row(key)
        elif kind == "status":
            self.status_var.set(R(d["text"]))
        elif kind == "stage":
            step = {"ocr": "قراءة OCR", "correct": "تصحيح", "photos": "وصف الصور"}.get(d["step"], d["step"])
            self._run["stage"] = f"الآن: {step} ص{d['pages']}"
            self._progress()
        elif kind == "log":
            self.log(d["text"])
        elif kind == "api_file":
            self.log(f"→ {d['name']}: {d['pages']} صفحة إلى {d['provider']} (دفعة {d['batch']})")
        elif kind == "review_needed":
            self.pending_review = d["items"]
        elif kind == "finished":
            stopped = d.get("stopped", False)
            switch, self._switch_mode = getattr(self, "_switch_mode", None), None
            if switch and stopped:
                self._set_running(False)
                self._set_mode(switch)
                self._refresh_privacy()
                self.log(f"⇄ الاستكمال بنمط «{dict(auto='تلقائي', local='داخلي', api='عبر المواقع').get(switch, switch)}»")
                self.root.after(200, self._start)
                return
            self._set_running(False)
            if d.get("index_only"):
                self.status_var.set(R("تم بناء الفهرس"))
                return
            self.bar.configure(value=0 if not stopped else self.bar["value"])
            r = self._run
            took = _fmt_elapsed(time.monotonic() - r["t0"]) if r["t0"] else ""
            summary = f"تمت معالجة {r['files']} مستند و{len(r['done'])} صفحة في {took}"
            self.status_var.set(R(("توقف — " + summary + ". شغّل مرة أخرى للإكمال من حيث توقف.") if stopped
                                  else summary))
            self.log(("تم الإيقاف. " if stopped else "انتهت الدفعة. ") + summary)
            self._show_done(True, stopped)
            self._warn_local_unverified()
            if self.pending_review:
                self._open_review()

    def _warn_local_unverified(self) -> None:
        """صفحات قرأها OCR الداخلي ويُشك فيها ولم يوجد مزوّد يؤكدها: المستخدم يتحقق بنفسه."""
        warn = {name: pages for name, pages in (self._run.get("local_warn") or {}).items() if pages}
        if not warn:
            return
        lines = []
        for name, pages in list(warn.items())[:8]:
            lines.append(f"• {Path(name).stem}:")
            lines += [f"    ص{n}: {why}" for n, why in sorted(pages.items(), key=lambda x: int(x[0]))[:6]]
            if len(pages) > 6:
                lines.append(f"    و{len(pages) - 6} صفحة أخرى")
        if len(warn) > 8:
            lines.append(f"و{len(warn) - 8} ملف آخر")
        self.log("⚠ صفحات تحتاج تحققك (لا يوجد مزوّد خارجي للتأكيد): "
                 + "؛ ".join(f"{Path(k).stem}: {', '.join(map(str, v))}" for k, v in warn.items()))
        messagebox.showwarning(
            "تحقق من هذه الصفحات",
            "قرأها المحرّك الداخلي، ويُرجّح وجود خطأ فيها، ولا يوجد مزوّد خارجي (OCR أو Gemini) للتأكيد:\n\n"
            + "\n".join(lines)
            + "\n\nالأرقام المشكوك فيها مكتوبة في أول كل صفحة داخل ملف الـ Markdown (⚠).\n"
              "للتأكيد التلقائي: شغّل الملف بوضع «عبر المواقع» — تُرسل هذه الصفحات وحدها.")

    @staticmethod
    def _file_summary(d: dict) -> str:
        parts = [f"{STATUS_LABELS.get(k, k)}: {v}" for k, v in sorted((d.get("counts") or {}).items())]
        converted = "؛ ".join(f"{v} صفحة: {k}" for k, v in (d.get("converted") or {}).items())
        if converted:
            parts.append(f"حُوّلت إلى OCR — {converted}")
        t = d.get("tables") or {}
        if t.get("detected"):
            parts.append(f"جداول: {t['detected']} (مدموجة: {t.get('merged', 0)})")
        if t.get("mismatched"):
            parts.append(f"⚠ جداول بتضارب أعمدة: {t['mismatched']}")
        if d.get("weak_count"):
            parts.append(f"⚠ صفحات ضعيفة: {d['weak_count']}")
        if d.get("weak_pages"):
            parts.append(f"⚠ تطابق ضعيف مع OCR: {', '.join(map(str, d['weak_pages']))}")
        return f"✓ {d['name']} — " + "، ".join(parts)

    def _on_close(self) -> None:
        if self.busy():
            if not messagebox.askyesno("خروج", "المعالجة جارية والتقدّم محفوظ.\nهل تريد الخروج؟"):
                return
            self.stop_event.set()
            self.worker.join(timeout=5)
        self.bg.stop.set()
        if self.settings_win is not None:
            self.settings_win.shutdown()
        if self.active_provider is not None:
            self.active_provider.close()
        self.root.destroy()


def _fmt_elapsed(sec: float) -> str:
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h} ساعة و{m} دقيقة"
    if m:
        return f"{m} دقيقة و{s} ثانية"
    return f"{s} ثانية"


def run() -> None:
    log_file = setup_logging()
    log.info("بدء التشغيل، السجل في %s", log_file)
    try:  # نص واضح على الشاشات المكبّرة (125%/150%) بدل الضبابية؛ لا يحتاج admin
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    threading.Thread(target=cleanup_work_dir, name="cleanup", daemon=True).start()
    root = tk.Tk()
    MainWindow(root)
    root.mainloop()
