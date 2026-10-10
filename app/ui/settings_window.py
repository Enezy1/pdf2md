"""نافذة الإعدادات: المزوّدات | داخلي | متقدم.

- المزوّدات: صف مختصر لكل مزوّد (الاسم، الحالة، تفعيل)؛ المفتاح يُحرَّر بفتح الصف، والتفاصيل في "تفاصيل".
  المزوّدات المتوقفة مطوية في الأسفل.
- داخلي: النموذج وتنزيله وحالة المحرّك، و"مختبر النماذج" لمقارنة النماذج على صفحات مختارة.
- متقدم: كل ما تبقى.
"""

from __future__ import annotations

import logging
import os
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog

import customtkinter as ctk

from .. import model_manager as mm
from .. import models_catalog as cat
from ..config import API_BATCH_SIZES, app_data_dir
from ..providers.catalog import ProviderSpec
from . import ctk_ui as ui
from . import theme, themes
from .theme import R, S1, S2, S3, S4, px

log = logging.getLogger(__name__)

TABS = ("providers", "local", "advanced")

FIELD_LABELS = {
    "limits": "الحدود", "card_required": "شرط البطاقة", "phone_verification": "التحقق بالهاتف",
    "data_training": "استخدام البيانات", "rpm": "حد الطلبات بالدقيقة", "arabic_engine": "محرّك العربية", "accepts_images": "قبول الصور", "base_url": "الرابط الأساسي",
    "key_url": "رابط المفتاح", "max_pages_per_call": "حد الصفحات لكل طلب", "default_model": "اسم النموذج",
}


def _ram_gb() -> float | None:
    """ذاكرة الجهاز بالـ GB (ويندوز)، أو None إن تعذّرت معرفتها."""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullTotalPhys / 1024 ** 3
    except Exception:
        return None


def _on_disk(files) -> tuple[int, int]:
    """(المنجز، الكلي) لملفات تنزيل: الملف المكتمل بحجمه، وإلا الجزء المنزّل (.part)."""
    done = total = 0
    for path, f in files:
        total += f.size
        if path.is_file():
            done += min(path.stat().st_size, f.size)
        else:
            part = path.with_name(path.name + ".part")
            if part.is_file():
                done += min(part.stat().st_size, f.size)
    return done, total


def _yn(v) -> str:
    return "نعم" if v else "لا"


class ScrollFrame(tk.Frame):
    """إطار قابل للتمرير بعجلة الفأرة، يتمدد عرضاً مع النافذة."""

    def __init__(self, parent):
        t = theme.tokens()
        super().__init__(parent, background=t["bg"], borderwidth=0, highlightthickness=0)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=t["bg"])
        self.sb = ctk.CTkScrollbar(self, command=self.canvas.yview, width=12, fg_color="transparent",
                                   button_color=t["line"], button_hover_color=t["strong"])
        outer = tk.Frame(self.canvas, background=t["bg"], borderwidth=0, highlightthickness=0)
        self.inner = tk.Frame(outer, background=t["bg"], borderwidth=0, highlightthickness=0)
        self.inner.pack(fill="both", expand=True, padx=px(S4 + S2), pady=px(S4))
        outer.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._win = self.canvas.create_window((0, 0), window=outer, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side="left", fill="y")
        self.canvas.pack(side="right", fill="both", expand=True)
        self.bind_all_wheel()

    def bind_all_wheel(self):
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _wheel(self, e):
        if self.inner.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-e.delta / 120), "units")


class InfoPopover:
    """نافذة "تفاصيل" صغيرة: تُفتح بالضغط فقط، لها شريط عنوان عادي (تُسحب وتُغلق بـ ×)."""

    def __init__(self, anchor: tk.Widget, build, title: str = ""):
        self.anchor, self.build, self.title = anchor, build, title
        self.win: tk.Toplevel | None = None

    def toggle(self):
        if self.win is not None and self.win.winfo_exists():
            self.hide()
            return
        self._show()

    def _show(self):
        w = self.win = tk.Toplevel(self.anchor)
        w.title(self.title)
        w.transient(self.anchor.winfo_toplevel())
        w.resizable(False, False)
        w.configure(background=theme.tokens()["card"])
        f = ui.Frame(w, background=theme.tokens()["card"])
        f.pack(fill="both", expand=True, padx=px(S3 + S1), pady=px(S3 + S1))
        self.build(f, self)
        w.update_idletasks()
        x = self.anchor.winfo_rootx() + self.anchor.winfo_width() + px(S1)
        y = self.anchor.winfo_rooty() - px(S2)
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        x = max(min(x, sw - w.winfo_width() - px(S2)), 0)
        y = max(min(y, sh - w.winfo_height() - px(48)), 0)
        w.geometry(f"+{x}+{y}")
        w.protocol("WM_DELETE_WINDOW", self.hide)
        w.bind("<Escape>", lambda e: (self.hide(), self.anchor.focus_set()))
        w.focus_set()

    def hide(self):
        if self.win is not None and self.win.winfo_exists():
            self.win.destroy()
        self.win = None


class SettingsWindow:
    def __init__(self, app, tab: str = ""):
        self.app = app
        self.settings = app.settings
        self.win = tk.Toplevel(app.root)
        self.win.title(R("الإعدادات"))
        theme.fit_window(self.win, px(780), px(620), px(560), px(420))
        theme.dark_titlebar(self.win)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.lab_thread: threading.Thread | None = None
        self.lab_stop = threading.Event()
        self.lab_events: list[tuple] = []
        self.lab_lock = threading.Lock()
        self._popovers: list[InfoPopover] = []

        self.win.configure(background=theme.tokens()["bg"])
        self.nb = ui.Tabs(self.win, list(zip(TABS, ("المزوّدات", "داخلي", "متقدم"))), ScrollFrame,
                          on_change=self._hide_popovers)
        self.nb.pack(fill="both", expand=True, padx=px(S3), pady=px(S3))
        self.frames = self.nb.frames
        # البطاقات المفتوحة تُغلق عند النقر في أي مكان آخر، أو تبديل التبويب، أو تحريك النافذة
        self.win.bind("<Button-1>", self._click_outside, add="+")
        self.win.bind("<Escape>", lambda e: self._hide_popovers(), add="+")
        self.win.bind("<Configure>", lambda e: self._hide_popovers() if e.widget is self.win else None, add="+")
        self._build_providers()
        self._build_local()
        self._build_advanced()
        self.show(tab)
        self._poll()

    # ------------------------------------------------------------ عام
    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def show(self, tab: str = "") -> None:
        if tab in TABS:
            self.nb.select(TABS.index(tab))
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def _hide_popovers(self) -> None:
        for p in self._popovers:
            p.hide()

    def _click_outside(self, e) -> None:
        # النقر على زر "تفاصيل" نفسه يتولاه toggle؛ أي نقرة أخرى في النافذة تغلق البطاقات
        for p in self._popovers:
            if not str(e.widget).startswith(str(p.anchor)):
                p.hide()

    def lab_busy(self) -> bool:
        return self.lab_thread is not None and self.lab_thread.is_alive()

    def close(self) -> None:
        if self.lab_busy():
            if not messagebox.askyesno("مختبر النماذج", "المقارنة جارية. إيقافها وإغلاق النافذة؟", parent=self.win):
                return
            self.lab_stop.set()
        self.shutdown()

    def shutdown(self) -> None:
        self.lab_stop.set()
        for p in self._popovers:
            p.hide()
        try:
            self.win.unbind_all("<MouseWheel>")
            self.win.destroy()
        except tk.TclError:
            pass
        self.app.on_settings_changed()

    def _section(self, parent, title: str, first: bool = False) -> tk.Frame:
        ui.Label(parent, text=R(title), style="Subtitle.TLabel").pack(anchor="e", pady=(0 if first else px(S4 + S2), px(S2)))
        f = ui.Frame(parent)
        f.pack(fill="x")
        return f

    # ============================================================ المزوّدات
    def _build_providers(self) -> None:
        root = self.frames["providers"].inner
        top = ui.Frame(root)
        top.pack(fill="x", pady=(0, px(S3)))
        ui.Label(top, text=R("المزوّدات"), style="Subtitle.TLabel").pack(side="right")
        ui.Button(top, text="+ إضافة مزوّد", command=self._add_custom).pack(side="left")
        ui.Para(root, style="Muted.TLabel", text=(
            "يُرسل البرنامج الصفحات إلى المزوّدات المفعّلة فقط، وفقط في وضع «عبر المواقع». "
            "اضغط «مفتاح» لإضافة مفتاح أو تغييره.")).pack(fill="x", pady=(0, px(S3)))
        self.prov_body = ui.Frame(root)
        self.prov_body.pack(fill="x")
        self._open_editor: str | None = None
        self._retired_shown = False
        self._render_providers()

    def _render_providers(self) -> None:
        for p in self._popovers:
            p.hide()
        self._popovers = []
        for w in self.prov_body.winfo_children():
            w.destroy()
        cfg = self.app.provider_config
        specs = sorted(cfg.specs.values(), key=lambda s: (s.retired, s.custom, list(cfg.specs).index(s.id)))
        # شبكة واحدة لكل الصفوف حتى تتحاذى الأعمدة (من اليمين: الاسم، الحالة، … ، تفعيل، مفتاح، تفاصيل)
        g = self.prov_body
        g.columnconfigure(3, weight=1)
        self._prov_row = 0
        live = [sp for sp in specs if not sp.retired]
        retired = [sp for sp in specs if sp.retired]
        for spec in live:
            self._provider_row(spec)
        if retired:
            label = f"مزوّدون متوقفون ({len(retired)}) " + ("▴" if self._retired_shown else "▾")
            ui.link(g, label, self._toggle_retired).grid(
                row=self._prov_row, column=0, columnspan=6, sticky="e", pady=(px(S3), 0))
            self._prov_row += 1
            if self._retired_shown:
                for spec in retired:
                    self._provider_row(spec)

    def _toggle_retired(self) -> None:
        self._retired_shown = not self._retired_shown
        self._render_providers()

    def _toggle_editor(self, pid: str) -> None:
        self._open_editor = None if self._open_editor == pid else pid
        self._render_providers()

    def _provider_row(self, spec: ProviderSpec) -> None:
        cfg, secrets = self.app.provider_config, self.app.secrets
        g, r = self.prov_body, self._prov_row
        st = cfg.state(spec.id)
        has_key = secrets.has_key(spec.id)
        can, _why = cfg.can_enable(spec.id)
        pad = {"pady": px(S1)}

        name = spec.name + (" (متوقف)" if spec.retired else "")
        ui.Label(g, text=R(name), anchor="e").grid(row=r, column=5, sticky="e", padx=(px(S2), 0), **pad)
        # الحالة بالكلمات لا بالرموز: تُقرأ ويفهم منها لماذا "تفعيل" معطّل
        if not can:
            chip, chip_style = _why, "Muted.TLabel"
        elif st.enabled and has_key:
            chip, chip_style = "مفعّل", "Ok.TLabel"
        elif has_key:
            chip, chip_style = "المفتاح محفوظ", "Status.TLabel"
        else:
            chip, chip_style = "بلا مفتاح", "Muted.TLabel"
        ui.Label(g, text=R(chip), style=chip_style, anchor="e").grid(row=r, column=4, sticky="e",
                                                                    padx=(px(S3), 0), **pad)
        editing = self._open_editor == spec.id
        r0 = r
        extra_vars = {}
        key_var = tk.StringVar()
        if editing:
            r += 1
            ed = ui.Frame(g)
            ed.grid(row=r, column=0, columnspan=6, sticky="ew", pady=(px(S1), px(S2)))
            krow = ui.Frame(ed)
            krow.pack(fill="x")
            ui.Label(krow, text=R("المفتاح الجديد" if has_key else "المفتاح")).pack(side="right")
            key_entry = ui.Entry(krow, textvariable=key_var, show="•")
            key_entry.pack(side="right", fill="x", expand=True, padx=px(S2))
            from .clipboard import paste_into
            for ef in spec.extra_fields or ():
                er = ui.Frame(ed)
                er.pack(fill="x", pady=(px(S1), 0))
                v = tk.StringVar(value=st.extra.get(ef["id"], ""))
                ui.Label(er, text=R(ef.get("label", ef["id"]))).pack(side="right")
                ui.Entry(er, textvariable=v, show="•" if ef.get("secret") else "").pack(
                    side="right", fill="x", expand=True, padx=px(S2))
                extra_vars[ef["id"]] = v

        def save():
            st.extra = {k: v.get().strip() for k, v in extra_vars.items()}
            new_key = key_var.get().strip()
            if new_key:
                secrets.set_key(spec.id, new_key)
                key_var.set("")
                cfg.on_key_saved(spec.id)  # يُفعَّل تلقائياً فقط إن لم تكن له بنود غير متحقق منها
            cfg.save()
            self._open_editor = None
            self.app.on_settings_changed()
            self._render_providers()

        if editing:
            def paste_and_save():
                paste_into(key_entry)
                if key_var.get().strip():
                    save()
            ui.Button(krow, text=R("لصق وحفظ"), style="Accent.TButton", command=paste_and_save).pack(side="left")
            ui.Button(krow, text=R("حفظ"), command=save).pack(side="left", padx=(0, px(S2)))
            key_entry.bind("<Return>", lambda e: save())
            key_entry.bind("<Escape>", lambda e: self._toggle_editor(spec.id))
            self.win.after_idle(key_entry.focus_set)

        en_var = tk.BooleanVar(value=st.enabled and has_key)

        def toggle():
            want = bool(en_var.get())
            if want and spec.unverified and not st.enabled:
                items = "، ".join(FIELD_LABELS.get(u, u) for u in spec.unverified)
                if not messagebox.askyesno("تفعيل", f"{spec.name}: بنود لم يُتحقق منها ({items}).\nتفعيله رغم ذلك؟",
                                           icon="warning", default="no", parent=self.win):
                    en_var.set(False)
                    return
            st.enabled = want
            cfg.save()
            self.app.on_settings_changed()

        if can and has_key:
            ui.Check(g, text="تفعيل", variable=en_var, command=toggle).grid(
                row=r0, column=2, padx=(px(S2), px(S3)), **pad)
        if can:
            ui.Button(g, text=R("إغلاق" if editing else "مفتاح"), width=7,
                       command=lambda: self._toggle_editor(spec.id)).grid(row=r0, column=1, padx=(px(S2), 0), **pad)
        info = ui.Button(g, text=R("تفاصيل"), width=7)
        info.grid(row=r0, column=0, sticky="w", **pad)
        pop = InfoPopover(info, lambda f, pop: self._provider_info(f, pop, spec), spec.name)
        info.configure(command=pop.toggle)
        self._popovers.append(pop)
        self._prov_row = r + 1

    def _provider_info(self, f: tk.Frame, pop: InfoPopover, spec: ProviderSpec) -> None:
        cfg, secrets, usage = self.app.provider_config, self.app.secrets, self.app.usage
        st = cfg.state(spec.id)
        wrap = px(340)

        font_of = ui.label_font

        def line(text, style="TLabel"):
            ui.Label(f, text=theme.wrap_rtl(text, font_of(style), wrap), style=style, justify="right",
                      anchor="e").pack(fill="x", pady=(0, px(S1)))

        ui.Label(f, text=R(spec.name), style="Subtitle.TLabel").pack(anchor="e", pady=(0, px(S1)))
        if spec.description:
            line(spec.description)
        used = usage.get(spec.id)
        rpd = f" من {spec.rpd}" if spec.rpd else ""
        approx = " (تقريبي)" if spec.approx_counter else ""
        ex = " — انتهت حصة اليوم" if usage.is_exhausted(spec.id) else ""
        line(f"اليوم: {used}{rpd} طلب{approx}{ex}", "Muted.TLabel")

        model_var = tk.StringVar(value=st.model or spec.default_model)
        if not spec.is_ocr:   # محرّك OCR لا يحتاج اسم نموذج
            grid = ui.Frame(f)
            grid.pack(fill="x", pady=(px(S2), 0))
            ui.Label(grid, text=R("النموذج"), style="Muted.TLabel").grid(row=0, column=1, sticky="e", padx=(px(S2), 0))
            ui.Entry(grid, textvariable=model_var, width=24).grid(row=0, column=0, sticky="e")
            grid.columnconfigure(0, weight=1)

        status_lbl = ui.Label(f, style="Muted.TLabel", justify="right", anchor="e")

        class _Status:  # نص الحالة يلتف يدوياً (انظر theme.wrap_rtl)
            @staticmethod
            def set(text):
                status_lbl.configure(text=theme.wrap_rtl(text, font_of("Muted.TLabel"), wrap))

        status = _Status()
        btns = ui.Frame(f)
        btns.pack(fill="x", pady=(px(S2), 0))

        def apply_fields() -> bool:
            st.model = model_var.get().strip() or spec.default_model
            cfg.save()
            return True

        def test():
            if not apply_fields():
                return
            key = secrets.get_key(spec.id)
            if not key:
                status.set("أدخل المفتاح واحفظه أولاً")
                return
            status.set("جارٍ الاختبار بصورة وهمية…")

            def work():
                from ..dispatcher import make_provider
                from ..model_picker import models_for, picker_for
                try:
                    model = picker_for(spec.id, models_for(spec, st)).current
                    prov = make_provider(spec, st, key, model)
                    try:
                        text = self.app.gate.probe(prov, model)
                    finally:
                        prov.close()
                    msg = "✓ يعمل" if text.strip() else "✓ متصل لكن الرد فارغ"
                except Exception as e:
                    msg = f"✗ لم ينجح الاتصال: {str(e)[:120] or type(e).__name__}"
                try:
                    self.win.after(0, lambda: status.set(msg))
                except tk.TclError:
                    pass

            threading.Thread(target=work, daemon=True).start()

        if spec.key_url:
            ui.Button(btns, text=R("احصل على المفتاح"), command=lambda: webbrowser.open(spec.key_url)).pack(side="right")
        ui.Button(btns, text=R("اختبار الاتصال"), command=test,
                   state="normal" if cfg.can_enable(spec.id)[0] else "disabled").pack(side="right", padx=px(S2))
        if not spec.is_ocr:
            ui.Button(btns, text=R("حفظ"), command=lambda: apply_fields() and status.set("حُفظ")).pack(side="left")
        more = ui.Frame(f)
        more.pack(fill="x", pady=(px(S1), 0))
        if secrets.has_key(spec.id):
            ui.link(more, "حذف المفتاح", lambda: (pop.hide(), self._delete_key(spec))).pack(side="right")
        if spec.custom:
            ui.link(more, "حذف المزوّد", lambda: (pop.hide(), self._remove(spec))).pack(side="right", padx=px(S3))
        status_lbl.pack(fill="x")

    def _delete_key(self, spec):
        if messagebox.askyesno("حذف المفتاح", f"حذف مفتاح {spec.name}؟\nيتوقف المزوّد حتى تضيف مفتاحاً جديداً.",
                               icon="warning", default="no", parent=self.win):
            self.app.secrets.delete_key(spec.id)
            self.app.provider_config.state(spec.id).enabled = False
            self.app.provider_config.save()
            self.app.on_settings_changed()
            self._render_providers()

    def _remove(self, spec):
        if messagebox.askyesno("حذف المزوّد", f"حذف المزوّد {spec.name} ومفتاحه؟",
                               icon="warning", default="no", parent=self.win):
            self.app.secrets.delete_key(spec.id)
            self.app.provider_config.remove_custom(spec.id)
            self.app.on_settings_changed()
            self._render_providers()

    def _add_custom(self):
        name = simpledialog.askstring("مزوّد مخصص", "الاسم:", parent=self.win)
        if not name:
            return
        url = simpledialog.askstring("مزوّد مخصص", "الرابط الأساسي بصيغة OpenAI\n(مثال: https://api.example.com/v1):",
                                     parent=self.win)
        if not url:
            return
        model = simpledialog.askstring("مزوّد مخصص", "اسم النموذج (يقبل الصور):", parent=self.win) or ""
        try:
            self.app.provider_config.add_custom(name, url, model)
        except ValueError as e:
            messagebox.showerror("تعذّرت إضافة المزوّد", theme.friendly_error(e), parent=self.win)
            return
        self._render_providers()

    # ============================================================ داخلي
    def _build_local(self) -> None:
        root = self.frames["local"].inner
        sec = self._section(root, "المحرّكات", first=True)
        ui.Para(sec, style="Muted.TLabel", text=(
            "تقرأ الصفحات المصوّرة على جهازك بلا إنترنت. نزّل محرّكاً ثم فعّله؛ وإذا كان المثبّت محرّكاً واحداً "
            "فهو المستخدم تلقائياً.")).pack(fill="x", pady=(0, px(S2)))
        self.dl_anchor = ui.Frame(sec)   # شريط العمليات العامة (نسخ من مجلد، اختبار) أعلى القائمة لا أسفلها
        self.dl_anchor.pack(fill="x")
        self.eng_body = ui.Frame(sec)
        self.eng_body.pack(fill="x")
        self.paddle_opts = ui.Frame(sec)
        r = ui.Frame(sec)
        r.pack(fill="x", pady=(px(S3), 0))
        ui.Button(r, text=R("اختيار مجلد التنصيب (فلاشة)"), command=self._import_model_folder).pack(side="right")
        self.ram_var = tk.StringVar()
        ram = _ram_gb()
        if ram:
            self.ram_var.set(R(f"ذاكرة جهازك: {ram:.0f}GB. PaddleOCR والنماذج الأصغر (3B–4B) أسرع على جهاز بلا معالج رسومي."))
        ui.Label(sec, textvariable=self.ram_var, style="Muted.TLabel", anchor="e").pack(fill="x", pady=(px(S2), 0))
        self.dl_row = ui.Frame(self.dl_anchor)
        self.dl_bar = ui.ProgressBar(self.dl_row, maximum=1000)
        ui.Button(self.dl_row, text=R("إلغاء"), command=lambda: self.app.bg.cancel()).pack(side="left")
        self.dl_bar.pack(side="right", fill="x", expand=True, padx=(0, px(S2)))
        self.dl_var = tk.StringVar()
        self.dl_label = ui.Label(self.dl_anchor, textvariable=self.dl_var, style="Muted.TLabel", anchor="e")
        self.row_var = tk.StringVar()     # تقدم تنزيل المحرّك داخل صفه
        self.row_bar = None
        self._bg_was_running = False
        self._bg_shape = None
        self._refresh_models()
        self._build_lab(root)

    def _refresh_models(self) -> None:
        """يرسم قائمة المحرّكات: نموذج تحت نموذج، ولكل واحد حالته وتنزيل/حذف وتفعيل."""
        from .. import engine_choice as ec
        from .. import paddle_engine as pe
        # القائمة تُبنى كاملة في إطار جديد ثم تُبدَّل بالقديمة دفعة واحدة: بدل أن تظهر الصفوف واحداً بعد آخر
        # (حركة توحي بخلل). وعرض الأعمدة ثابت فلا تنزاح الأزرار حين يتحول صف إلى شريط تنزيل.
        old = self.eng_body
        g = ui.Frame(old.master)
        g.columnconfigure(3, weight=1)
        for col, w in ((0, 130), (1, 120), (2, 110), (4, 190)):
            g.columnconfigure(col, minsize=px(w))
        mode, active_spec = ec.effective(self.settings)
        pad = {"pady": px(S1)}
        rows = [("paddle", "PaddleOCR (تجريبي)", None)] + [(m.id, m.label, m) for m in cat.MODELS]
        exe = mm.llama_exe()
        for i, (eid, name, spec) in enumerate(rows):
            if spec is None:
                why = pe.unsupported_reason()
                installed = pe.is_installed()
                usable = installed and not why
                active = mode in (ec.PADDLE, ec.PADDLE_LOCAL)
                size = pe.installed_size() if installed else pe.total_size()
                note = why
            else:
                installed = mm.is_installed(spec)
                usable = installed and bool(exe)
                active = mode in (ec.LOCAL, ec.PADDLE_LOCAL) and active_spec is not None and active_spec.id == spec.id
                size = spec.total_size
                note = "" if (exe or not installed) else "ينقص محرّك llama-server"
            ui.Label(g, text=R(name), anchor="e").grid(row=i, column=5, sticky="e", padx=(px(S2), 0), **pad)
            bg = self.app.bg
            if bg.key == eid and (bg.running or bg.paused):
                self._download_row(g, i, pad)
                continue
            if active:
                chip, style = "مفعّل", "Ok.TLabel"
            elif note:
                chip, style = note, "Err.TLabel" if spec is None else "Muted.TLabel"
            elif installed:
                chip, style = f"مثبّت · {self._size(size)}", "Status.TLabel"
            else:
                chip, style = f"غير مثبّت · {self._size(size)}", "Muted.TLabel"
            ui.Label(g, text=R(chip), style=style, anchor="e").grid(row=i, column=4, sticky="e", padx=(px(S3), 0), **pad)
            if usable:
                v = tk.BooleanVar(value=active)
                ui.Check(g, text="تفعيل", variable=v, command=lambda e=eid, var=v: self._activate(e, var)).grid(
                    row=i, column=2, padx=(px(S2), px(S3)), **pad)
            if eid == "paddle" and usable:
                ui.Button(g, text=R("اختبار سريع"), command=self._paddle_quick_test).grid(row=i, column=0, sticky="w", **pad)
            if installed or (spec is None and pe.root().exists()):
                ui.Button(g, text=R("حذف"), width=7, command=lambda e=eid, s=spec: self._delete_engine(e, s)).grid(
                    row=i, column=1, padx=(px(S2), 0), **pad)
            elif not (spec is None and why):
                ui.Button(g, text=R("تنزيل"), width=7, style="Accent.TButton",
                          command=lambda e=eid, s=spec: self._download_engine(e, s)).grid(
                    row=i, column=1, padx=(px(S2), 0), **pad)
            if spec is not None and installed and not exe:
                ui.Button(g, text=R("تنزيل llama-server"), command=self._download_llama).grid(
                    row=i, column=0, sticky="w", **pad)
        g.pack(fill="x", after=old)
        old.destroy()
        self.eng_body = g
        self._render_paddle_opts(mode)
        if hasattr(self, "lab_models_frame"):
            self._render_lab_models()

    def _download_row(self, g, i: int, pad: dict) -> None:
        """صف المحرّك أثناء تنزيله: شريط ونسبة ومتبقٍ، وإيقاف مؤقت/استئناف وإلغاء."""
        bg = self.app.bg
        box = ui.Frame(g)
        box.grid(row=i, column=2, columnspan=3, sticky="ew", padx=(px(S3), 0), **pad)
        self.row_bar = ui.ProgressBar(box, maximum=1000)
        self.row_bar.pack(fill="x")
        ui.Label(box, textvariable=self.row_var, style="Muted.TLabel", anchor="e").pack(fill="x")
        if bg.paused and not bg.running:
            ui.Button(g, text=R("استئناف"), width=8, style="Accent.TButton",
                      command=lambda: (bg.resume(), self._refresh_models())).grid(
                row=i, column=1, padx=(px(S2), 0), **pad)
        else:
            ui.Button(g, text=R("إيقاف مؤقت"), width=9, command=bg.pause).grid(
                row=i, column=1, padx=(px(S2), 0), **pad)
        ui.Button(g, text=R("إلغاء"), width=6,
                  command=lambda: (bg.cancel(), self._refresh_models())).grid(row=i, column=0, sticky="w", **pad)
        self._update_row_progress()

    def _update_row_progress(self) -> None:
        bg = self.app.bg
        done, total, eta = bg.overall()
        frac = done / total if total else 0
        if self.row_bar is not None:
            try:
                self.row_bar.configure(value=int(frac * 1000))
            except tk.TclError:
                self.row_bar = None
        verifying = bg.label.startswith("تحقق")
        if bg.paused and not bg.running:
            state = "متوقف مؤقتاً"
        elif verifying:
            state = "التحقق من الملف (البصمة)…"
        else:
            state = "جارٍ التنزيل"
        left = f" · باقي {self._eta(eta)}" if eta and bg.running and not verifying else ""
        amount = f" · {self._size(done)} من {self._size(total)}" if total else ""
        self.row_var.set(R(f"{state} {frac:.0%}{amount}{left}" if total else state + "…"))

    @staticmethod
    def _eta(sec: float) -> str:
        sec = int(sec)
        if sec < 60:
            return f"{sec} ث"
        if sec < 3600:
            return f"{sec // 60} د"
        return f"{sec // 3600} س و{sec % 3600 // 60} د"

    @staticmethod
    def _size(n: int) -> str:
        return f"{n / 1e9:.1f}GB" if n >= 1e9 else f"{n / 1e6:.0f}MB"

    def _render_paddle_opts(self, mode: str) -> None:
        """خيارات PaddleOCR تظهر تحت القائمة فقط حين يكون مفعّلاً."""
        from .. import engine_choice as ec
        for w in self.paddle_opts.winfo_children():
            w.destroy()
        if mode not in (ec.PADDLE, ec.PADDLE_LOCAL):
            self.paddle_opts.pack_forget()
            return
        self.paddle_opts.pack(fill="x", pady=(px(S2), 0), after=self.eng_body)
        s = self.settings
        has_local = ec.ready_llama_spec(s) is not None
        v = tk.BooleanVar(value=s.scan_engine == ec.PADDLE_LOCAL and has_local)
        if has_local:
            ui.Check(self.paddle_opts, text=theme.ck("الصفحات الضعيفة تُعاد على النموذج المحلي"), variable=v,
                     command=lambda: self._set_paddle_mode(ec.PADDLE_LOCAL if v.get() else ec.PADDLE)).pack(anchor="e")
        r = ui.Frame(self.paddle_opts)
        r.pack(fill="x", pady=(px(S1), 0))
        ui.Label(r, text=R("أقل ثقة مقبولة للسطر")).pack(side="right")
        tv = tk.StringVar(value=f"{s.paddle_min_conf:.2f}")
        e = ui.Entry(r, textvariable=tv, width=7)
        e.pack(side="right", padx=px(S2))

        def save_conf(_e=None):
            try:
                self._set("paddle_min_conf", min(max(float(tv.get()), 0.0), 1.0))
            except ValueError:
                pass
            tv.set(f"{self.settings.paddle_min_conf:.2f}")
        e.bind("<FocusOut>", save_conf)
        e.bind("<Return>", save_conf)
        ui.Para(self.paddle_opts, style="Muted.TLabel", text=(
            "الأسطر الأقل ثقة تُعلَّم بـ [؟] وتظهر في تقرير المراجعة، أو تُعاد صفحتها على النموذج المحلي "
            "إن فعّلت الخيار أعلاه.")).pack(fill="x", pady=(px(S1), 0))

    def _set_paddle_mode(self, mode: str) -> None:
        self._set("scan_engine", mode)
        self._engines_changed()

    def _activate(self, eid: str, var: tk.BooleanVar) -> None:
        """التفعيل اختيار واحد: تفعيل محرّك يلغي الآخر. إلغاء علامة المفعّل لا يفعل شيئاً."""
        if not var.get():
            var.set(True)
            return
        if eid == "paddle":
            self.settings.scan_engine = "paddle"
        else:
            self.settings.local_model = eid
            self.settings.scan_engine = "local"
        self.settings.save()
        self._engines_changed()

    def _engines_changed(self) -> None:
        self._refresh_models()
        self.app.on_settings_changed()  # يحدّث حالة المحرّك في الصفحة الرئيسية

    def _bg(self, title: str, fn, key: str = "", measure=None) -> None:
        bg = self.app.bg
        if bg.running or bg.paused:
            messagebox.showinfo(title, "توجد عملية تنزيل جارية أو متوقفة مؤقتاً. أكملها أو ألغها أولاً.",
                                parent=self.win)
            return
        bg.start(title, fn, key, measure)
        self._refresh_models()

    def _download_engine(self, eid: str, spec: cat.ModelSpec | None) -> None:
        if spec is None:
            self._paddle_download()
            return
        if mm.is_installed(spec):
            return
        if not messagebox.askyesno(
                "تنزيل", f"تنزيل {spec.label} ({spec.total_size / 1e9:.1f}GB) من:\nhuggingface.co/{spec.repo}\n\n"
                         "مع التحقق من البصمة. يمكن الإلغاء والاستئناف لاحقاً.", parent=self.win):
            return
        need_llama = mm.llama_exe() is None

        def work(pr, stop):
            if need_llama and mm.llama_exe() is None:
                mm.download_llama(pr, stop)
            mm.download_model(spec, pr, stop)
            return f"تم تنزيل {spec.label} والتحقق منه."

        files = list(zip(mm.model_paths(spec), (spec.model, spec.mmproj)))
        if need_llama:
            files.append((mm.llama_root() / cat.LLAMA_ZIP.name, cat.LLAMA_ZIP))
        self._bg("تنزيل النموذج", work, spec.id, lambda: _on_disk(files))

    def _download_llama(self) -> None:
        if mm.llama_exe():
            return
        if not messagebox.askyesno(
                "تنزيل", f"تنزيل محرّك llama.cpp {cat.LLAMA_TAG} ({cat.LLAMA_ZIP.size / 1e6:.0f}MB) من GitHub؟",
                parent=self.win):
            return
        self._bg("تنزيل المحرّك", lambda pr, st: f"تم التثبيت: {mm.download_llama(pr, st)}")

    def _import_model_folder(self) -> None:
        """مجلد (فلاشة) فيه ملفات نماذج محلية و/أو ملفات PaddleOCR — يُنسخ كل ما يُعرف منها مع التحقق."""
        from .. import paddle_engine as pe
        folder = filedialog.askdirectory(title="مجلد النماذج (فلاشة مثلاً)", parent=self.win)
        if not folder:
            return

        def work(pr, st):
            rep = mm.import_from_folder(Path(folder), pr, st)
            lines = [f"نماذج منسوخة ومتحقق منها: {', '.join(rep['models']) or 'لا شيء'}"]
            if rep["llama"]:
                lines.append("المحرّك: نُسخ وتحقق منه")
            lines += rep["skipped"]
            if not pe.is_installed() and not pe.unsupported_reason() and pe.files_in(Path(folder)):
                try:
                    lines.append(pe.install_from_folder(Path(folder), pr, st))
                except pe.PaddleError as e:
                    lines.append(f"PaddleOCR: {e}")
            if not rep["models"] and not rep["llama"] and not rep["skipped"] and len(lines) == 1:
                lines.append("لم يُعثر على ملفات معروفة (الأسماء يجب أن تطابق الكتالوج).")
            return "\n".join(lines)

        self._bg("نسخ من مجلد", work)

    def _delete_engine(self, eid: str, spec: cat.ModelSpec | None) -> None:
        if self.app.busy() or self.app.bg.running or self.lab_busy():
            messagebox.showinfo("حذف", "انتظر حتى تنتهي العملية الجارية.", parent=self.win)
            return
        if spec is None:
            self._paddle_delete()
            return
        size = next((s for mid, _l, s in mm.installed_folders() if mid == spec.id), spec.total_size)
        if not messagebox.askyesno("حذف النموذج", f"حذف {spec.label} ({size / 1e9:.2f}GB) نهائياً من الجهاز؟\n"
                                   "لا يمكن التراجع؛ ستحتاج تنزيله من جديد.",
                                   icon="warning", default="no", parent=self.win):
            return
        try:
            freed = mm.delete_model(spec.id)
        except Exception as e:
            log.exception("تعذّر حذف النموذج")
            messagebox.showerror("تعذّر حذف النموذج", theme.friendly_error(e), parent=self.win)
            return
        self.app.log(f"حُذف النموذج {spec.label} — تحرر {freed / 1e9:.2f}GB")
        self._engines_changed()

    def _poll_bg(self) -> None:
        bg = self.app.bg
        shape = (bg.key, bg.running, bg.paused)
        if bg.key and (bg.running or bg.paused):
            if shape != self._bg_shape:          # تغيّر الزر (إيقاف مؤقت ↔ استئناف): يُعاد رسم القائمة
                self._bg_shape = shape
                self._refresh_models()
            self._update_row_progress()
            self._bg_was_running = bg.running
            return
        self._bg_shape = shape
        if bg.running:
            if not self.dl_row.winfo_ismapped():
                self.dl_row.pack(fill="x", pady=(px(S2), 0))
                self.dl_label.pack(fill="x")
            frac = bg.done / bg.total if bg.total else 0
            self.dl_bar.configure(value=int(frac * 1000))
            self.dl_var.set(R(f"{bg.label}: {bg.done / 1e6:.0f} من {bg.total / 1e6:.0f}MB") if bg.total else R(bg.title + "…"))
            self._bg_was_running = True
        elif self._bg_was_running:
            self._bg_was_running = False
            self.dl_row.pack_forget()
            self.dl_label.pack_forget()
            self._engines_changed()
            if bg.error:
                messagebox.showerror(f"فشل: {bg.title}", bg.error, parent=self.win)
            elif bg.result:
                messagebox.showinfo(bg.title, bg.result, parent=self.win)
            self.app.log(f"{bg.title}: {bg.error or bg.result}")

    # ------------------------------------------------------------ PaddleOCR (تجريبي)
    def _paddle_download(self) -> None:
        from .. import paddle_engine as pe
        if not messagebox.askyesno(
                "تنزيل PaddleOCR",
                f"تنزيل PaddleOCR ({pe.total_size() / 1e6:.0f}MB):\n"
                "• نموذجا PP-OCRv5 (كشف + عربي) من modelscope.cn (RapidAI/RapidOCR)\n"
                "• onnxruntime وnumpy وopencv وpyclipper من pypi.org\n\n"
                f"إلى {pe.root()}\nمع التحقق من الحجم والبصمة. يمكن الإلغاء والاستئناف لاحقاً.", parent=self.win):
            return
        files = [(pe.root() / "downloads" / i.file.name, i.file) for i in pe.ITEMS]
        self._bg("تنزيل PaddleOCR", lambda pr, st: pe.install(pr, st), "paddle",
                 lambda: (pe.total_size(), pe.total_size()) if pe.is_installed() else _on_disk(files))

    def _paddle_quick_test(self) -> None:
        from .. import paddle_engine as pe

        def work(pr, st):
            r = pe.quick_test()
            head = "✓ نجح الاختبار" if r["ok"] else "✗ لم تُقرأ: " + "، ".join(r["missing"])
            return (f"{head}\nالزمن: {r['seconds']:.1f} ث (القراءة {r['ocr_ms'] / 1000:.1f} ث)\n\n"
                    f"النتيجة:\n{r['text']}")

        self._bg("اختبار PaddleOCR", work)

    def _paddle_delete(self) -> None:
        from .. import paddle_engine as pe
        if not messagebox.askyesno("حذف PaddleOCR", f"حذف PaddleOCR ({pe.installed_size() / 1e6:.0f}MB) نهائياً؟\n"
                                   f"يُمسح المجلد {pe.root()} بالكامل وتعود إعداداته للافتراضي.",
                                   icon="warning", default="no", parent=self.win):
            return
        try:
            freed = pe.uninstall(self.settings)
        except Exception as e:
            log.exception("تعذّر حذف PaddleOCR")
            messagebox.showerror("تعذّر الحذف", theme.friendly_error(e), parent=self.win)
            return
        self.app.log(f"حُذف PaddleOCR — تحرر {freed / 1e6:.0f}MB")
        self._engines_changed()

    def _reopen(self) -> None:
        tab = TABS[self.nb.current()]
        self.shutdown()
        self.app.settings_win = None
        self.app.open_settings(tab)

    # ------------------------------------------------------------ مختبر النماذج
    def _build_lab(self, root) -> None:
        sec = self._section(root, "مختبر النماذج")
        ui.Para(sec, style="Muted.TLabel", text=(
            "قارن النماذج على صفحات من ملفك. للحصول على نسبة الدقة اختر PDF نصياً: يقرأ النموذج صورة الصفحة "
            "ونقارن ناتجه بالنص الأصلي.")).pack(fill="x", pady=(0, px(S2)))

        r = ui.Frame(sec)
        r.pack(fill="x")
        ui.Button(r, text=R("اختيار PDF"), command=self._lab_pick_pdf).pack(side="right")
        self.lab_pdf_var = tk.StringVar(value="")
        self.lab_pdf: Path | None = None
        ui.Label(r, textvariable=self.lab_pdf_var, style="Muted.TLabel", anchor="e").pack(side="right", fill="x", expand=True, padx=px(S2))

        r = ui.Frame(sec)
        r.pack(fill="x", pady=(px(S2), 0))
        ui.Label(r, text=R("الصفحات")).pack(side="right")
        self.lab_pages = tk.StringVar(value="1")
        ui.Entry(r, textvariable=self.lab_pages, width=10).pack(side="right", padx=px(S2))
        ui.Label(r, text=R("وضوح الصورة")).pack(side="right", padx=(px(S3), 0))
        self.lab_dpi = {d: tk.BooleanVar(value=d == 200) for d in (150, 200, 300)}
        for d, v in self.lab_dpi.items():
            ui.Check(r, text=theme.ck(str(d)), variable=v).pack(side="right", padx=(px(S1), 0))
        r = ui.Frame(sec)
        r.pack(fill="x", pady=(px(S2), 0))
        self.lab_ref_var = tk.StringVar(value=R("النص الصحيح للمقارنة: النص المنسوخ في الملف نفسه"))
        self.lab_ref: Path | None = None
        ui.Label(r, textvariable=self.lab_ref_var, style="Muted.TLabel").pack(side="right")
        ui.link(r, "ملف نصي آخر…", self._lab_pick_ref).pack(side="right", padx=px(S3))

        ui.Label(sec, text=R("النماذج"), anchor="e").pack(fill="x", pady=(px(S2), 0))
        self.lab_models_frame = ui.Frame(sec)
        self.lab_models_frame.pack(fill="x")
        self.lab_model_vars: dict[str, tk.BooleanVar] = {}
        self._render_lab_models()

        r = ui.Frame(sec)
        r.pack(fill="x", pady=(px(S3), 0))
        self.lab_btn = ui.Button(r, text=R("قارن"), style="Accent.TButton", width=10, command=self._lab_start_or_stop)
        self.lab_btn.pack(side="right")
        self.lab_bar = ui.ProgressBar(r, maximum=1000)
        self.lab_bar.pack(side="right", fill="x", expand=True, padx=(0, px(S3)))
        self.lab_status = tk.StringVar()
        ui.Label(sec, textvariable=self.lab_status, style="Status.TLabel", anchor="e").pack(fill="x", pady=(px(S1), 0))

        r = ui.Frame(sec)
        r.pack(fill="x", pady=(px(S2), px(S1)))
        self.lab_view = tk.StringVar(value="now")
        ui.Segmented(r, [("now", "هذا التشغيل", ""), ("all", "كل التشغيلات", "")], self.lab_view,
                     self._lab_show_results).pack(side="right")
        self.lab_report_btn = ui.Button(r, text=R("فتح التقرير"), command=self._lab_open_report, state="disabled")
        self.lab_report_btn.pack(side="left")
        self.lab_tree = ui.ResultsTable(sec, (
            ("sec", "ثوانٍ/صفحة", 90, False), ("quality", "الجودة", 70, False), ("acc", "الدقة", 70, False),
            ("dpi", "الوضوح", 70, False), ("model", "النموذج", 240, True), ("rank", "الترتيب", 60, False)))
        self.lab_tree.pack(fill="x")
        ui.Para(sec, style="Muted.TLabel", text=(
            "الدقة: التطابق مع النص المرجعي. الجودة: مؤشرات البرنامج للصفحة الضعيفة (تكرار، علامات [ ]، "
            "كلمات مجهولة، نص قصير).")).pack(fill="x", pady=(px(S1), 0))
        self.lab_report = None
        self.lab_html: Path | None = None
        self._lab_show_results()

    def _render_lab_models(self) -> None:
        for w in self.lab_models_frame.winfo_children():
            w.destroy()
        installed = [m for m in cat.MODELS if mm.is_installed(m)]
        if not installed:
            ui.Label(self.lab_models_frame, text=R("لا توجد نماذج مثبّتة بعد — نزّل نموذجاً من الأعلى."),
                      style="Muted.TLabel", anchor="e").pack(fill="x")
            return
        cols = 2
        for i, m in enumerate(installed):
            v = self.lab_model_vars.setdefault(m.id, tk.BooleanVar(value=True))
            ui.Check(self.lab_models_frame, text=theme.ck(m.label), variable=v).grid(
                row=i // cols, column=cols - 1 - i % cols, sticky="e", padx=px(S2), pady=px(S1 / 2))
        for c in range(cols):
            self.lab_models_frame.columnconfigure(c, weight=1)

    def _lab_pick_pdf(self) -> None:
        p = filedialog.askopenfilename(title="ملف للمقارنة", filetypes=[("PDF", "*.pdf")], parent=self.win)
        if p:
            self.lab_pdf = Path(p)
            try:
                import pymupdf as fitz
                with fitz.open(p) as d:
                    n = d.page_count
                self.lab_pdf_var.set(R(f"{self.lab_pdf.name} — {n} صفحة"))
            except Exception:
                self.lab_pdf_var.set(R(self.lab_pdf.name))

    def _lab_pick_ref(self) -> None:
        p = filedialog.askopenfilename(
            title="النص الصحيح (صفحات مفصولة بسطر: === صفحة 3 ===)",
            filetypes=[("نص", "*.txt *.md"), ("الكل", "*.*")], parent=self.win)
        if p:
            self.lab_ref = Path(p)
            self.lab_ref_var.set(R(f"النص الصحيح للمقارنة: {self.lab_ref.name}"))
        else:
            self.lab_ref = None
            self.lab_ref_var.set(R("النص الصحيح للمقارنة: النص المنسوخ في الملف نفسه"))

    def _lab_start_or_stop(self) -> None:
        if self.lab_busy():
            self.lab_stop.set()
            self.lab_btn.configure(state="disabled")
            self.lab_status.set(R("جارٍ الإيقاف…"))
            return
        from .. import model_lab as lab
        if self.app.busy() or self.app.bg.running:
            messagebox.showinfo("مختبر النماذج", "انتظر حتى تنتهي المعالجة أو التنزيل.", parent=self.win)
            return
        if not self.lab_pdf or not self.lab_pdf.is_file():
            messagebox.showinfo("مختبر النماذج", "اختر ملف PDF.", parent=self.win)
            return
        if not mm.llama_exe():
            messagebox.showinfo("مختبر النماذج", "المحرّك غير مثبّت. اضغط «تنزيل المحرّك».", parent=self.win)
            return
        models = [m for m in cat.MODELS if mm.is_installed(m) and self.lab_model_vars.get(m.id, tk.BooleanVar()).get()]
        dpis = tuple(d for d, v in self.lab_dpi.items() if v.get())
        if not models or not dpis:
            messagebox.showinfo("مختبر النماذج", "اختر نموذجاً واحداً ووضوح صورة واحداً على الأقل.", parent=self.win)
            return
        import pymupdf as fitz
        try:
            with fitz.open(self.lab_pdf) as d:
                pages = lab.parse_pages(self.lab_pages.get(), d.page_count)
        except Exception as e:
            messagebox.showerror("أرقام الصفحات غير صالحة",
                                 theme.friendly_error(e) + "\nاكتبها هكذا: 1، 3-5", parent=self.win)
            return
        report = lab.Report(self.lab_pdf.name, pages)
        try:
            if self.lab_ref:
                report.reference, report.reference_source = lab.parse_reference_file(self.lab_ref), self.lab_ref.name
            else:
                report.reference = lab.text_layer_reference(self.lab_pdf, pages, self.settings.text_min_chars)
                report.reference_source = "طبقة النص في الملف" if report.reference else ""
        except Exception as e:
            messagebox.showerror("تعذّرت قراءة الملف المرجعي", theme.friendly_error(e), parent=self.win)
            return
        total = len(models) * len(dpis) * len(pages)
        self.lab_stop = threading.Event()
        self.lab_report, self.lab_html = report, None
        self._lab_progress = {"done": 0, "total": total}
        self.lab_bar.configure(value=0)
        self.lab_btn.configure(text=R("إيقاف"), style="TButton")
        self.lab_report_btn.configure(state="disabled")
        self.lab_view.set("now")
        if not report.reference:
            self.lab_status.set(R("بدون نص مرجعي: ستظهر الجودة والزمن فقط."))
        pdf = self.lab_pdf

        def emit(*ev):
            with self.lab_lock:
                self.lab_events.append(ev)

        def work():
            from ..dictionary import ArabicDictionary
            stopped = False
            try:
                lab.run(pdf, pages, models, report, log=lambda t: emit("log", t), stop=self.lab_stop, dpis=dpis,
                        on_cell=lambda *a: emit("cell", a), dictionary=ArabicDictionary())
            except lab.Stopped:
                stopped = True
            except Exception as e:
                log.exception("فشل مختبر النماذج")
                emit("log", f"✗ {e}")
            try:
                lab.save_history(report)
                out = lab.history_path().parent / f"report-{datetime.now():%Y%m%d-%H%M%S}.html"
                out.write_text(lab.render_html(report), encoding="utf-8")
                emit("done", stopped, out)
            except Exception as e:
                log.exception("تعذّر حفظ تقرير المختبر")
                emit("done", stopped, None)
                emit("log", f"✗ {e}")

        self.lab_thread = threading.Thread(target=work, daemon=True)
        self.lab_thread.start()

    def _poll_lab(self) -> None:
        with self.lab_lock:
            evs, self.lab_events = self.lab_events, []
        for ev in evs:
            if ev[0] == "log":
                text = ev[1].strip()
                if text.startswith("—"):
                    self.lab_status.set(R(text.strip("— ")))
                elif "✗" in text:
                    self.lab_status.set(R(text))
                self.app.log("مختبر: " + text)
            elif ev[0] == "cell":
                self._lab_progress["done"] += 1
                p = self._lab_progress
                self.lab_bar.configure(value=int(p["done"] / max(p["total"], 1) * 1000))
                mid, dpi, page, cell = ev[1]
                label = cat.get(mid).label
                self.lab_status.set(R(f"{label} — صفحة {page} بوضوح {dpi}: {cell.seconds} ثانية ({p['done']} من {p['total']})"))
                self._lab_show_results()
            elif ev[0] == "done":
                stopped, out = ev[1], ev[2]
                self.lab_html = out
                self.lab_btn.configure(text=R("قارن"), style="Accent.TButton", state="normal")
                self.lab_report_btn.configure(state="normal" if out else "disabled")
                if not stopped:
                    self.lab_bar.configure(value=1000)
                self.lab_status.set(R("توقف" if stopped else "تم"))
                self._lab_show_results()

    def _lab_show_results(self) -> None:
        from .. import model_lab as lab
        self.lab_tree.delete(*self.lab_tree.get_children())
        pct = lab._pct
        if self.lab_view.get() == "all":
            for i, r in enumerate(lab.history_summary(lab.load_history()), 1):
                sec = "—" if r["seconds"] is None else f"{r['seconds']:.1f}"
                self.lab_tree.insert("", "end", values=(sec, pct(r["quality"]), pct(r["accuracy"]), r["dpi"],
                                                        f"{r['label']}  ×{r['runs']}", i))
            return
        if self.lab_report is None:
            return
        for i, r in enumerate(lab.ranking(self.lab_report), 1):
            sec = "—" if r.seconds is None else f"{r.seconds:.1f}"
            self.lab_tree.insert("", "end", values=(sec, pct(r.quality), pct(r.accuracy), r.dpi, r.label, i))

    def _lab_open_report(self) -> None:
        if self.lab_html and self.lab_html.is_file():
            os.startfile(self.lab_html)  # noqa: S606

    # ============================================================ متقدم
    def _build_advanced(self) -> None:
        root = self.frames["advanced"].inner
        s = self.settings

        # ---- الأساسيات: ما يحتاجه المستخدم فعلاً
        sec = self._section(root, "مكان ملفات Markdown", first=True)
        r = ui.Frame(sec)
        r.pack(fill="x")
        ui.Button(r, text=R("تغيير"), command=self._choose_output).pack(side="right")
        ui.Button(r, text=R("افتراضي"), command=lambda: self._set_output("")).pack(side="right", padx=px(S2))
        self.out_lbl = ui.PathLabel(r, theme.tokens()['bg'], theme.tokens()['muted'])
        self.out_lbl.pack(side="right", fill="x", expand=True, padx=(0, px(S2)))
        self._show_output()

        sec = self._section(root, "خيارات")
        self._checks(sec, (
            ("البحث في المجلدات الفرعية", "include_subfolders"),
            ("بناء الفهرس تلقائياً بعد كل دفعة", "auto_build_index"),
        ))

        sec = self._section(root, "المظهر")
        r = ui.Frame(sec)
        r.pack(fill="x")
        ui.Label(r, text=R("الألوان")).pack(side="right")
        names = {"system": "حسب النظام", "light": "فاتح", "dark": "داكن"}
        tv = tk.StringVar(value=names.get(s.theme, names["system"]))
        cb = ui.Combo(r, textvariable=tv, values=list(names.values()), width=12, state="readonly")
        cb.pack(side="right", padx=px(S2))
        cb.bind("<<ComboboxSelected>>", lambda e: self._set_theme({v: k for k, v in names.items()}[tv.get()]))
        r2 = ui.Frame(sec)
        r2.pack(fill="x", pady=(px(S2), 0))
        ui.Label(r2, text=R("ثيم الألوان")).pack(side="right")
        pal = {k: v["label"] for k, v in themes.THEMES.items()}
        pv = tk.StringVar(value=pal.get(s.palette or themes.DEFAULT, pal[themes.DEFAULT]))
        pcb = ui.Combo(r2, textvariable=pv, values=list(pal.values()), width=22, state="readonly")
        pcb.pack(side="right", padx=px(S2))
        pcb.bind("<<ComboboxSelected>>", lambda e: self._set_palette(
            {v: k for k, v in pal.items()}[pv.get()]))

        sec = self._section(root, "اختصارات لوحة المفاتيح")
        for keys, what in (("Ctrl+Enter", "ابدأ / إيقاف"), ("Ctrl+O", "اختيار مجلد"), ("Ctrl+Shift+O", "إضافة ملف"),
                           ("Ctrl+,", "الإعدادات"), ("مسافة", "تحديد الملف أو إلغاؤه"),
                           ("Shift+F10", "قائمة الملف"), ("Delete", "إزالة ملف مضاف من القائمة")):
            r = ui.Frame(sec)
            r.pack(fill="x")
            ui.Label(r, text=R(what)).pack(side="right")
            ui.Label(r, text=theme.ltr(keys), style="Muted.TLabel").pack(side="left")

        # ---- خيارات إضافية: مطوية افتراضياً (لا تحتاجها عادة)
        more = ui.Frame(root)

        def flip():
            if more.winfo_ismapped():
                more.pack_forget()
                toggle.configure(text=R("خيارات إضافية ▾"))
            else:
                more.pack(fill="x", after=toggle)
                toggle.configure(text=R("خيارات إضافية ▴"))
        toggle = ui.link(root, "خيارات إضافية ▾", flip)
        toggle.pack(anchor="e", pady=(px(S4 + S2), 0))

        sec = self._section(more, "القراءة")
        self._checks(sec, (
            ("تجاهل النص المنسوخ في الملفات وقراءة كل الصفحات كصور", "force_ocr_all"),
            ("تأكيد الصفحات النصية بقراءتها كصورة أيضاً (أبطأ)", "verify_text_with_ocr"),
            ("استخراج الجداول", "extract_tables"),
        ))
        form = ui.Frame(sec)
        form.pack(fill="x", pady=(px(S2), 0))
        form.columnconfigure(0, weight=1)
        rows = (
            ("وضوح الصورة — داخلي", "render_dpi", (150, 200, 300)),
            ("وضوح الصورة — عبر المواقع", "api_dpi", (100, 150, 200)),
            ("صفحات في كل طلب — عبر المواقع", "api_pages_per_call", API_BATCH_SIZES),
        )
        for i, (label, attr, values) in enumerate(rows):
            ui.Label(form, text=R(label)).grid(row=i, column=1, sticky="e", pady=px(S1))
            v = tk.StringVar(value=str(getattr(s, attr)))
            c = ui.Combo(form, textvariable=v, values=values, width=6, state="readonly")
            c.grid(row=i, column=0, sticky="w", pady=px(S1))
            c.bind("<<ComboboxSelected>>", lambda e, a=attr, var=v: self._set(a, int(var.get())))

        sec = self._section(more, "عبر المواقع")
        self._checks(sec, (
            ("كتابة ملخص الفهرس عبر المواقع", "summary_via_api"),
            ("إرسال الصفحات الضعيفة تلقائياً دون سؤال", "auto_send_weak"),
            ("تصحيح نص OCR بنموذج نصي (المصحّح)", "corrector_enabled"),
            ("إرسال الصور الصعبة مع النص لنموذج رؤية", "send_hard_images"),
            ("توحيد الأرقام إلى إنجليزية", "normalize_digits"),
            ("وصف الصور داخل المستندات ونسخ المحادثات", "describe_images"),
            ("حذف ذيل الصفحة المتكرر وأرقام الصفحات والتوقيعات", "strip_page_furniture"),
        ))
        form = ui.Frame(sec)
        form.pack(fill="x", pady=(px(S2), 0))
        form.columnconfigure(0, weight=1)
        engines = {"auto": "تلقائي حسب المفاتيح", "ocrspace": "OCR.space ثم المصحّح", "vision": "نموذج الرؤية مباشرة"}
        ui.Label(form, text=R("محرّك OCR")).grid(row=0, column=1, sticky="e", pady=px(S1))
        ev = tk.StringVar(value=engines.get(s.ocr_engine, engines["auto"]))
        ecb = ui.Combo(form, textvariable=ev, values=list(engines.values()), width=22, state="readonly")
        ecb.grid(row=0, column=0, sticky="w", pady=px(S1))
        ecb.bind("<<ComboboxSelected>>", lambda e: self._set("ocr_engine", {v: k for k, v in engines.items()}[ev.get()]))
        ui.Label(form, text=R("محرّك OCR.space")).grid(row=1, column=1, sticky="e", pady=px(S1))
        nv = tk.StringVar(value=str(s.ocrspace_engine))
        ncb = ui.Combo(form, textvariable=nv, values=(3, 2), width=6, state="readonly")
        ncb.grid(row=1, column=0, sticky="w", pady=px(S1))
        ncb.bind("<<ComboboxSelected>>", lambda e: self._set("ocrspace_engine", int(nv.get())))
        ui.link(sec, "إظهار تنبيه «عبر المواقع» مرة أخرى",
                   lambda: (self._set("cloud_notice_dismissed", False),
                            messagebox.showinfo("تم", "سيظهر التنبيه في المرة القادمة.", parent=self.win))
                   ).pack(anchor="e", pady=(px(S1), 0))

        sec = self._section(more, "أدوات")
        r = ui.Frame(sec)
        r.pack(fill="x")
        ui.Button(r, text=R("بناء الفهرس"), command=lambda: self.app.build_index(False)).pack(side="right")
        ui.Button(r, text=R("إعادة بناء الفهرس كاملاً"), command=lambda: self.app.build_index(True)).pack(
            side="right", padx=px(S2))
        r = ui.Frame(sec)
        r.pack(fill="x", pady=(px(S2), 0))
        ui.Button(r, text=R("سجل الإرسال"), command=self._open_sent_log).pack(side="right")
        ui.Button(r, text=R("تصدير معلومات التشخيص"), command=self._export_diag).pack(side="right", padx=px(S2))

    def _checks(self, parent, items) -> None:
        for label, attr in items:
            v = tk.BooleanVar(value=bool(getattr(self.settings, attr)))
            ui.Check(parent, text=theme.ck(label), variable=v,
                            command=lambda a=attr, var=v: self._set(a, bool(var.get()))).pack(anchor="e", pady=px(S1 / 2))

    def _set(self, attr: str, value) -> None:
        setattr(self.settings, attr, value)
        self.settings.save()
        if attr == "include_subfolders" and self.settings.last_folder:
            self.app._load_folder(Path(self.settings.last_folder))

    def _set_int(self, attr: str, var: tk.StringVar, lo: int) -> None:
        try:
            self._set(attr, max(lo, int(var.get())))
        except ValueError:
            var.set(str(getattr(self.settings, attr)))

    def _show_output(self) -> None:
        out = self.settings.output_dir
        self.out_lbl.set(out if out else "بجانب ملف PDF", "" if self.settings.output_dir else "(افتراضي)")

    def _set_output(self, path: str) -> None:
        if self.app.busy():
            return
        self._set("output_dir", path)
        self._show_output()

    def _choose_output(self) -> None:
        d = filedialog.askdirectory(title="مجلد الإخراج", parent=self.win)
        if d:
            self._set_output(d)

    def _open_sent_log(self) -> None:
        p = app_data_dir() / "sent_log.csv"
        if p.is_file():
            os.startfile(p)  # noqa: S606
        else:
            messagebox.showinfo("سجل الإرسال", "لم يُرسل شيء بعد.", parent=self.win)

    def _export_diag(self) -> None:
        path = filedialog.asksaveasfilename(
            title="تصدير معلومات التشخيص", defaultextension=".zip", parent=self.win,
            initialfile=f"pdf2md-diagnostics-{datetime.now():%Y%m%d}.zip", filetypes=[("ZIP", "*.zip")])
        if not path:
            return
        from ..diagnostics import export
        try:
            export(Path(path))
        except Exception as e:
            log.exception("فشل تصدير التشخيص")
            messagebox.showerror("تعذّر تصدير التشخيص", theme.friendly_error(e), parent=self.win)
            return
        messagebox.showinfo("التشخيص", "تم. الملف لا يحتوي مفاتيح ولا نصوص مستندات.", parent=self.win)

    def _set_palette(self, name: str) -> None:
        self._set("palette", name)
        self._set_theme(self.settings.theme)

    def _set_theme(self, mode: str) -> None:
        self._set("theme", mode)
        theme.apply(self.app.root, mode, self.settings.palette)
        self.app.rebuild_ui()
        self._reopen()

    # ------------------------------------------------------------ الحلقة
    def _poll(self) -> None:
        if not self.alive():
            return
        try:
            self._poll_bg()
            self._poll_lab()
        except tk.TclError:
            return
        self.win.after(200, self._poll)
