"""عناصر الواجهة المبنية على CustomTkinter بتصميم Studio: أزرار، مفتاح وضع، شارات، جدول الملفات.

كل الألوان من theme.palette()/theme.tokens() (themes.py). النصوص العربية تمرّ عبر R() كما في بقية الواجهة،
والترتيب من اليمين إلى اليسار يدوي (pack side="right" / أعمدة grid معكوسة).
أحجام ودجات CTk تُمرَّر بلا px(): المكتبة تضربها بمقياس الشاشة بنفسها؛ أما عناصر tk العادية فتمرّ عبر px().
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

import customtkinter as ctk

from . import theme
from .theme import R, fs, px

# خط Fluent للأيقونات (ويندوز 11) مع بديل ويندوز 10
_ICON_FONTS = ("Segoe Fluent Icons", "Segoe MDL2 Assets")
ICON = {
    "settings": "", "folder": "", "add": "", "trash": "", "history": "",
    "doc": "", "more": "", "check": "", "cloud": "", "shield": "",
    "upload": "", "arrow": "", "layers": "", "chip": "",
}

_fam: dict[str, str] = {}


def _first_family(candidates: tuple[str, ...], fallback: str) -> str:
    have = set(tkfont.families())
    return next((c for c in candidates if c in have), fallback)


def text_family() -> str:
    if "text" not in _fam:
        _fam["text"] = _first_family(("IBM Plex Arabic", "IBM Plex Sans Arabic"), theme.FONT)
    return _fam["text"]


def icon_family() -> str:
    if "icon" not in _fam:
        _fam["icon"] = _first_family(_ICON_FONTS, "Segoe UI Symbol")
    return _fam["icon"]


def font(size: int = 14, weight: str = "normal") -> ctk.CTkFont:
    """حجم بالبكسل (يُضرب في "حجم النص" في ويندوز ثم في مقياس الشاشة داخل CTk)."""
    return ctk.CTkFont(family=text_family(), size=max(int(round(size * theme._text)), 9), weight=weight)


def icon_font(size: int = 16) -> ctk.CTkFont:
    return ctk.CTkFont(family=icon_family(), size=int(round(size * theme._text)))


def _tkfont(size: int, weight: str = "normal", icons: bool = False) -> tuple:
    """خط tk عادي (بالنقاط عبر fs) للعناصر الخفيفة داخل صفوف الجدول."""
    return (icon_family() if icons else text_family(), -px(size * theme._text), weight)


def mix(a: str, b: str, f: float) -> str:
    return theme._mix(a, b, f)


_imgs: dict = {}


def check_images() -> dict[str, tk.PhotoImage]:
    key = (theme.palette_name(), theme.is_dark(), id(tk._default_root))
    if _imgs.get("key") != key:
        _imgs["key"], _imgs["imgs"] = key, make_check_images()
    return _imgs["imgs"]


class CheckRow(tk.Frame):
    """مربع اختيار مرسوم + نص على يساره (تخطيط من اليمين لليسار)، مرتبط بـ BooleanVar."""

    def __init__(self, parent, text: str, variable: tk.BooleanVar, command, bg: str):
        super().__init__(parent, background=bg, borderwidth=0, highlightthickness=0, cursor="hand2", takefocus=1)
        t = theme.tokens()
        self.var, self._cmd = variable, command
        self.img = tk.Label(self, background=bg, borderwidth=0)
        self.img.pack(side="right")
        self.lbl = tk.Label(self, text=R(text), background=bg, foreground=t["fg"], font=_tkfont(14), borderwidth=0)
        self.lbl.pack(side="right", padx=(0, px(8)))
        for w in (self, self.img, self.lbl):
            w.bind("<Button-1>", lambda e: self._toggle())
        self.bind("<space>", lambda e: (self._toggle(), "break")[1])
        self.refresh()

    def _toggle(self) -> None:
        self.var.set(not self.var.get())
        self._cmd()
        self.refresh()

    def refresh(self) -> None:
        self.img.configure(image=check_images()["on" if self.var.get() else "off"])


class PathLabel(tk.Label):
    """مسار من اليسار لليمين يُختصر من الوسط حسب العرض المتاح."""

    def __init__(self, parent, bg: str, fg: str, size: int = 13):
        super().__init__(parent, background=bg, foreground=fg, anchor="w", borderwidth=0,
                         font=("Consolas", -px(size * theme._text)))
        self._path, self._suffix, self._width = "", "", 0
        self.bind("<Configure>", self._on_resize)

    def set(self, path: str, suffix: str = "") -> None:
        self._path, self._suffix = path, suffix
        self._render()

    def _on_resize(self, e) -> None:
        if abs(e.width - self._width) > 4:
            self._width = e.width
            self._render()

    def _render(self) -> None:
        if not self._path:
            self.configure(text="")
            return
        f = tkfont.Font(font=self.cget("font"))
        extra = f.measure(self._suffix + "   ") if self._suffix else 0
        w = max((self._width or px(400)) - px(8) - extra, px(80))
        text = theme.ltr(theme.shorten_path(self._path, f, w))
        self.configure(text=f"{text}   {R(self._suffix)}" if self._suffix else text)


class Btn(ctk.CTkFrame):
    """زر بإطار مدوّر وأيقونة اختيارية. kind: primary | outline | ghost.
    الأيقونة في البداية (يمين) كما في الواجهات العربية، وarrow_end تضعها في النهاية (يسار)."""

    def __init__(self, parent, text: str = "", command=None, icon: str = "", kind: str = "outline",
                 height: int = 38, radius: int = 8, icon_end: bool = False, min_width: int = 0, size: int = 14):
        super().__init__(parent, height=height, corner_radius=radius, border_width=1, width=min_width or 40)
        self._cmd, self._kind, self._state, self._hover = command, kind, "normal", False
        self._text, self._icon, self._icon_end, self._size = text, icon, icon_end, size
        self.pack_propagate(False)
        self._build_inner()
        self._paint()
        self._bind_all(self)
        self._min_width = min_width
        self._fit(min_width)

    def _build_inner(self) -> None:
        for w in self.winfo_children():
            w.destroy()
        self._icon_lbl = self._text_lbl = None
        inner = tk.Frame(self, borderwidth=0, highlightthickness=0)
        self._inner = inner
        inner.place(relx=0.5, rely=0.5, anchor="center")
        order = [("text", self._text)] if self._text else []
        if self._icon:
            order.insert(0 if not self._icon_end else len(order), ("icon", self._icon))
        # الترتيب البصري من اليمين: الأول يُحزَم أولاً على اليمين
        for kind, val in order:
            lbl = tk.Label(inner, text=R(val) if kind == "text" else val, borderwidth=0,
                           font=_tkfont(self._size, "bold") if kind == "text" else _tkfont(self._size + 2, icons=True))
            lbl.pack(side="right", padx=(px(6) if (self._text and self._icon) else 0, 0))
            setattr(self, f"_{kind}_lbl", lbl)
        for w in (inner, *inner.winfo_children()):
            self._bind_all(w)

    def _fit(self, min_width: int) -> None:
        # قياس مباشر من عرض الملصقات: update_idletasks هنا كانت تعيد رسم كل النافذة لكل زر (~40ms) فتبطؤ الإعدادات
        lbls = [l for l in (self._icon_lbl, self._text_lbl) if l is not None]
        gap = px(6) if len(lbls) == 2 else 0
        need = int((sum(l.winfo_reqwidth() for l in lbls) + gap) / max(self._get_widget_scaling(), 0.1)) + 36
        self.configure(width=max(min_width, need))

    def _bind_all(self, w) -> None:
        b = (lambda seq, fn: ctk.CTkFrame.bind(self, seq, fn, add="+")) if w is self else \
            (lambda seq, fn: w.bind(seq, fn, add="+"))
        b("<Enter>", lambda e: self._set_hover(True))
        b("<Leave>", lambda e: self._set_hover(False))
        b("<Button-1>", lambda e: self._click())

    def bind(self, sequence=None, command=None, add=None):
        """الربط على الإطار وعلى كل ما بداخله (الإطار وحده لا يستقبل الأحداث فوق نصه)."""
        ctk.CTkFrame.bind(self, sequence, command, add="+")
        for w in (self._inner, *self._inner.winfo_children()):
            w.bind(sequence, command, add="+")

    def _set_hover(self, v: bool) -> None:
        self._hover = v
        self._paint()

    def _click(self) -> None:
        if self._state == "normal" and self._cmd:
            self._cmd()

    def _colors(self) -> tuple[str, str, str]:
        t = theme.tokens()
        if self._state != "normal":
            return t["raised"], t["line"], t["disabled"]
        if self._kind == "primary":
            f = mix(t["accent"], "#ffffff", 0.1) if self._hover else t["accent"]
            return f, f, t["on_accent"]
        if self._kind == "ghost":
            return (t["raised"] if self._hover else t["card"]), t["line"], t["muted"]
        return (t["select"] if self._hover else t["raised"]), (t["strong"] if self._hover else t["line"]), t["fg"]

    def _paint(self) -> None:
        fill, border, fg = self._colors()
        ctk.CTkFrame.configure(self, fg_color=fill, border_color=border)
        self._inner.configure(background=fill)
        for lbl in (self._icon_lbl, self._text_lbl):
            if lbl is not None:
                lbl.configure(background=fill, foreground=fg)
        try:
            tk.Frame.configure(self, cursor="hand2" if self._state == "normal" else "arrow")
        except tk.TclError:
            pass

    def configure(self, require_redraw=False, **kw):
        if "state" in kw:
            self._state = kw.pop("state")
        if "kind" in kw:
            self._kind = kw.pop("kind")
        if "style" in kw:
            self._kind = "primary" if kw.pop("style") == "Accent.TButton" else "outline"
        if "text" in kw:
            self._text = _plain(kw.pop("text"))
            self._build_inner()
            self._fit(self._min_width)
        if "command" in kw:
            self._cmd = kw.pop("command")
        if kw:
            super().configure(require_redraw=require_redraw, **kw)
        self._paint()

    def cget(self, attribute_name: str):
        if attribute_name == "state":
            return self._state
        return super().cget(attribute_name)

    def refresh(self) -> None:
        self._paint()


class Segmented(ctk.CTkFrame):
    """مفتاح وضع من خيارين أو أكثر؛ كل خيار قابل للتعطيل وحده. buttons[value] يقبل configure/cget(state)."""

    def __init__(self, parent, options: list[tuple[str, str, str]], variable: tk.StringVar, command=None):
        super().__init__(parent, corner_radius=10, border_width=1)
        self.var, self._command = variable, command
        self.buttons: dict[str, ctk.CTkButton] = {}
        t = theme.tokens()
        ctk.CTkFrame.configure(self, fg_color=t["card"], border_color=t["line"])
        for value, text, _icon in options:
            b = ctk.CTkButton(self, text=R(text), font=font(15, "bold"), corner_radius=8, height=38, border_width=0,
                              command=lambda v=value: self._pick(v), width=120)
            b.pack(side="right", padx=4, pady=4)
            self.buttons[value] = b
        variable.trace_add("write", lambda *_: self.refresh())
        self.refresh()

    def _pick(self, value: str) -> None:
        if self.buttons[value].cget("state") != "normal":
            return
        self.var.set(value)
        if self._command:
            self._command()

    def refresh(self) -> None:
        t = theme.tokens()
        ctk.CTkFrame.configure(self, fg_color=t["card"], border_color=t["line"])
        for value, b in self.buttons.items():
            on = self.var.get() == value
            b.configure(fg_color=t["accent"] if on else "transparent", hover_color=t["accent"] if on else t["select"],
                        text_color=t["on_accent"] if on else t["fg"], text_color_disabled=t["disabled"])


class ProgressBar(ctk.CTkProgressBar):
    """CTkProgressBar بواجهة configure(value=) و["value"] على مقياس 0..maximum كما في ttk.Progressbar."""

    def __init__(self, parent, maximum: int = 1000, **kw):
        super().__init__(parent, height=10, corner_radius=5, **kw)
        self._max, self._value = maximum, 0
        self.refresh()
        ctk.CTkProgressBar.set(self, 0)

    def configure(self, require_redraw=False, **kw):
        if "value" in kw:
            self._value = kw.pop("value")
            t = theme.tokens()
            # عند الصفر لا نرسم بقعة التقدّم (CTk يرسم نقطة صغيرة بقيمة 0)
            super().configure(progress_color=t["accent"] if self._value > 0 else t["line"])
            ctk.CTkProgressBar.set(self, min(max(self._value / self._max, 0), 1))
        if kw:
            super().configure(require_redraw=require_redraw, **kw)

    def __getitem__(self, key):
        return self._value if key == "value" else self.cget(key)

    def refresh(self) -> None:
        t = theme.tokens()
        super().configure(fg_color=t["line"], progress_color=t["accent"] if self._value > 0 else t["line"])


def make_check_images() -> dict[str, tk.PhotoImage]:
    """مربع اختيار مرسوم (محدد / غير محدد) بألوان الثيم، بحدود ناعمة."""
    from PIL import Image, ImageDraw, ImageTk

    t = theme.tokens()
    size, k = px(20), 4
    out = {}
    for name in ("on", "off"):
        im = Image.new("RGBA", (size * k, size * k), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        r = px(5) * k
        if name == "on":
            d.rounded_rectangle((0, 0, size * k - 1, size * k - 1), r, fill=t["accent"])
            pts = [(0.27, 0.52), (0.44, 0.69), (0.74, 0.33)]
            d.line([(x * size * k, y * size * k) for x, y in pts], fill=t["on_accent"], width=int(size * k * 0.12),
                   joint="curve")
        else:
            d.rounded_rectangle((k, k, size * k - 1 - k, size * k - 1 - k), r, fill=t["raised"], outline=t["strong"],
                                width=2 * k)
        out[name] = ImageTk.PhotoImage(im.resize((size, size), Image.LANCZOS))
    return out


class FileTable(tk.Frame):
    """جدول الملفات: ترويسة + صفوف قابلة للتمرير + منطقة "إضافة ملفات" في النهاية.

    واجهة قريبة من Treeview لما يستخدمه main_window: insert / exists / delete / get_children / set_row /
    selection. الأعمدة من اليمين: مربع، أيقونة+اسم (يتمدّد)، الصفحات، الحالة، قائمة الصف (أقصى اليسار).
    """

    COLS = {"check": 56, "pages": 110, "status": 150, "more": 52}

    def __init__(self, parent, on_toggle, on_menu, on_remove, on_add):
        t = theme.tokens()
        super().__init__(parent, background=t["bg"], borderwidth=0, highlightthickness=0)
        self.on_toggle, self.on_menu, self.on_remove, self.on_add = on_toggle, on_menu, on_remove, on_add
        self._order: list[str] = []
        self._rows: dict[str, dict] = {}
        self._sel: list[str] = []
        self._anchor: str | None = None
        self.imgs = check_images()
        self.card = ctk.CTkFrame(self, corner_radius=12, border_width=1, fg_color=t["card"], border_color=t["line"])
        self.card.pack(fill="both", expand=True)
        self._header()
        self.holder = tk.Frame(self.card, background=t["card"], borderwidth=0, highlightthickness=0)
        self.holder.pack(fill="both", expand=True, padx=2, pady=(0, 6))
        self.canvas = tk.Canvas(self.holder, background=t["card"], highlightthickness=0, borderwidth=0)
        self.sb = ctk.CTkScrollbar(self.holder, command=self.canvas.yview, width=12,
                                   fg_color="transparent", button_color=t["line"], button_hover_color=t["strong"])
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side="left", fill="y")
        self.canvas.pack(side="right", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, background=t["card"], borderwidth=0, highlightthickness=0)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.rows_frame = tk.Frame(self.inner, background=t["card"], borderwidth=0, highlightthickness=0)
        self.rows_frame.pack(fill="x")
        self._dropzone()
        for w in (self.canvas, self.inner, self.rows_frame):
            w.bind("<MouseWheel>", self._wheel)
        self.canvas.configure(takefocus=1)
        for seq, fn in (("<space>", self._key_space), ("<Up>", lambda e: self._key_move(-1)),
                        ("<Down>", lambda e: self._key_move(1)), ("<Delete>", self._key_delete),
                        ("<Shift-F10>", self._key_menu), ("<App>", self._key_menu)):
            self.canvas.bind(seq, fn)

    # ---------------------------------------------------------------- الهيكل
    def _header(self) -> None:
        t = theme.tokens()
        bar = tk.Frame(self.card, background=t["card"], borderwidth=0, highlightthickness=0)
        bar.pack(fill="x", padx=2, pady=(2, 0))
        self._hdr = bar
        self._grid_cols(bar)
        for col, text in ((2, "اسم الملف"), (3, "الصفحات"), (1, "الحالة")):
            tk.Label(bar, text=R(text), background=t["card"], foreground=t["muted"],
                     font=_tkfont(14, "bold")).grid(row=0, column=col, sticky="e" if col == 2 else "", pady=px(12),
                                                    padx=px(8))
        line = tk.Frame(self.card, background=t["line"], height=1)
        line.pack(fill="x", padx=2)
        self._hdr_line = line

    def _grid_cols(self, f: tk.Frame) -> None:
        # الأعمدة من اليسار: قائمة الصف، الحالة، الصفحات، الاسم (يتمدّد)، مربع الاختيار
        f.columnconfigure(0, minsize=px(self.COLS["more"]))
        f.columnconfigure(1, minsize=px(self.COLS["status"]))
        f.columnconfigure(2, weight=1)
        f.columnconfigure(3, minsize=px(self.COLS["pages"]))
        f.columnconfigure(4, minsize=px(self.COLS["check"]))

    def _dropzone(self) -> None:
        t = theme.tokens()
        self.zone = tk.Canvas(self.inner, background=t["card"], highlightthickness=0, height=px(130), cursor="hand2")
        self.zone.pack(fill="x", padx=px(14), pady=px(14))
        self.zone.bind("<Configure>", lambda e: self._paint_zone())
        self.zone.bind("<Button-1>", lambda e: self.on_add())
        self.zone.bind("<MouseWheel>", self._wheel)
        self._zone_title = self._zone_sub = ""

    def set_zone_text(self, title: str, sub: str) -> None:
        self._zone_title, self._zone_sub = title, sub
        self._paint_zone()

    def _paint_zone(self) -> None:
        t = theme.tokens()
        z = self.zone
        z.delete("all")
        z.configure(background=t["card"])
        w, h = z.winfo_width(), z.winfo_height()
        if w < 20:
            return
        z.create_rectangle(2, 2, w - 3, h - 3, outline=t["strong"], dash=(5, 4), width=1)
        cx, cy = w // 2, h // 2 - px(34)
        r = px(24)
        z.create_oval(cx - r, cy - r, cx + r, cy + r, fill=t["raised"], outline=t["line"])
        z.create_text(cx, cy, text=ICON["upload"], font=_tkfont(18, icons=True), fill=t["muted"])
        z.create_text(cx, cy + px(42), text=R(self._zone_title), font=_tkfont(14, "bold"), fill=t["fg"])
        z.create_text(cx, cy + px(66), text=R(self._zone_sub), font=_tkfont(12), fill=t["muted"])

    # ---------------------------------------------------------------- واجهة شبيهة بـ Treeview
    def insert(self, _parent, _where, iid: str) -> None:
        self._order.append(iid)
        self._build_row(iid)

    def exists(self, key: str) -> bool:
        return key in self._rows

    def get_children(self) -> tuple[str, ...]:
        return tuple(self._order)

    def delete(self, *keys: str) -> None:
        for k in keys:
            r = self._rows.pop(k, None)
            if r:
                r["frame"].destroy()
                self._order.remove(k)
                if k in self._sel:
                    self._sel.remove(k)

    def selection(self) -> tuple[str, ...]:
        return tuple(self._sel)

    def set_row(self, key: str, name: str, pages: str, status: str, sub: str, checked: bool, tone: str) -> None:
        r = self._rows.get(key)
        if not r:
            return
        t = theme.tokens()
        r["chk"].configure(image=self.imgs["on" if checked else "off"])
        r["name"].configure(text=R(name), foreground=t["fg"] if checked else t["muted"])
        r["sub"].configure(text=R(sub))
        r["pages"].configure(text=pages or "—")
        col = {"ok": t["ok"], "err": t["err"], "busy": t["accent_text"]}.get(tone, t["muted"])
        r["status"].configure(text=R(status), text_color=col, fg_color=mix(col, t["card"], 0.80))

    # ---------------------------------------------------------------- الصفوف
    def _build_row(self, key: str) -> None:
        t = theme.tokens()
        f = tk.Frame(self.rows_frame, background=t["card"], borderwidth=0, highlightthickness=0)
        f.pack(fill="x")
        self._grid_cols(f)
        chk = tk.Label(f, image=self.imgs["off"], background=t["card"], cursor="hand2", borderwidth=0)
        chk.grid(row=0, column=4, padx=px(8), pady=px(12))
        pair = tk.Frame(f, background=t["card"], borderwidth=0, highlightthickness=0)
        pair.grid(row=0, column=2, sticky="e", padx=px(8))
        box = ctk.CTkFrame(pair, width=42, height=42, corner_radius=10, border_width=1,
                           fg_color=mix(t["accent"], t["card"], 0.86), border_color=mix(t["accent"], t["card"], 0.65))
        box.pack(side="right", padx=(px(12), 0))
        tk.Label(box, text=ICON["doc"], font=_tkfont(19, icons=True), foreground=t["accent_text"],
                 background=mix(t["accent"], t["card"], 0.86), borderwidth=0).place(relx=0.5, rely=0.5, anchor="center")
        textcol = tk.Frame(pair, background=t["card"], borderwidth=0, highlightthickness=0)
        textcol.pack(side="right")
        name = tk.Label(textcol, anchor="e", background=t["card"], foreground=t["fg"], font=_tkfont(17, "bold"),
                        borderwidth=0)
        name.pack(anchor="e")
        sub = tk.Label(textcol, anchor="e", background=t["card"], foreground=t["muted"], font=_tkfont(12),
                       borderwidth=0)
        sub.pack(anchor="e")
        pages = tk.Label(f, background=t["raised"], foreground=t["fg"], font=_tkfont(15, "bold"), borderwidth=0,
                         padx=px(14), pady=px(4))
        pages.grid(row=0, column=3)
        status = ctk.CTkLabel(f, text="", corner_radius=14, height=30, font=font(14, "bold"), padx=16)
        status.grid(row=0, column=1)
        more = tk.Label(f, text=ICON["more"], font=_tkfont(16, icons=True), background=t["card"],
                        foreground=t["muted"], cursor="hand2", borderwidth=0)
        more.grid(row=0, column=0, padx=px(10))
        sep = tk.Frame(self.rows_frame, background=t["line"], height=1)
        sep.pack(fill="x", padx=px(12))
        self._rows[key] = {"frame": f, "sep": sep, "chk": chk, "name": name, "sub": sub, "pages": pages,
                           "status": status, "more": more, "bg": [f, pair, textcol, name, sub, chk, more]}
        # الربط: النقر على المربع يبدّل التحديد، على الصف يحدّده، وعلى ⋯ أو بزر الفأرة الأيمن القائمة
        chk.bind("<Button-1>", lambda e, k=key: (self.on_toggle(k), "break")[1])
        more.bind("<Button-1>", lambda e, k=key: self._menu(k, e))
        for w in (f, pair, textcol, name, sub, pages, box, *box.winfo_children(), status):
            w.bind("<Button-1>", lambda e, k=key: self._click(k, e), add="+")
            w.bind("<Button-3>", lambda e, k=key: self._menu(k, e), add="+")
            w.bind("<MouseWheel>", self._wheel, add="+")
        for w in (chk, more):
            w.bind("<MouseWheel>", self._wheel, add="+")
        self._paint_row(key)

    def _paint_row(self, key: str) -> None:
        r = self._rows.get(key)
        if not r:
            return
        t = theme.tokens()
        bg = t["select"] if key in self._sel else t["card"]
        r["frame"].configure(background=bg)
        for w in r["bg"]:
            w.configure(background=bg)
        r["sep"].configure(background=t["line"])

    def _click(self, key: str, e) -> None:
        self.canvas.focus_set()
        if e.state & 0x4:  # Ctrl
            self._sel = [k for k in self._sel if k != key] if key in self._sel else self._sel + [key]
        elif e.state & 0x1 and self._anchor in self._order:  # Shift
            a, b = sorted((self._order.index(self._anchor), self._order.index(key)))
            self._sel = self._order[a:b + 1]
        else:
            self._sel = [key]
            self._anchor = key
        for k in self._order:
            self._paint_row(k)

    def select_only(self, key: str) -> None:
        self._sel, self._anchor = [key], key
        for k in self._order:
            self._paint_row(k)

    def _menu(self, key: str, e) -> str:
        if key not in self._sel:
            self.select_only(key)
        self.on_menu(key, e.x_root, e.y_root)
        return "break"

    def _wheel(self, e) -> None:
        self.canvas.yview_scroll(int(-e.delta / 120) * 2, "units")

    # ---------------------------------------------------------------- لوحة المفاتيح
    def _key_space(self, _e):
        for k in list(self._sel):
            self.on_toggle(k)
        return "break"

    def _key_move(self, d: int):
        if not self._order:
            return "break"
        cur = self._order.index(self._sel[-1]) if self._sel else (-1 if d > 0 else len(self._order))
        key = self._order[min(max(cur + d, 0), len(self._order) - 1)]
        self.select_only(key)
        y = self._rows[key]["frame"].winfo_y()
        h = self._rows[key]["frame"].winfo_height()
        top, bot = self.canvas.canvasy(0), self.canvas.canvasy(self.canvas.winfo_height())
        total = max(self.inner.winfo_height(), 1)
        if y < top:
            self.canvas.yview_moveto(y / total)
        elif y + h > bot:
            self.canvas.yview_moveto((y + h - self.canvas.winfo_height()) / total)
        return "break"

    def _key_delete(self, _e):
        self.on_remove(list(self._sel))
        return "break"

    def _key_menu(self, _e):
        if self._sel:
            r = self._rows[self._sel[0]]["frame"]
            self.on_menu(self._sel[0], r.winfo_rootx() + r.winfo_width() // 2, r.winfo_rooty() + r.winfo_height())
        return "break"

    def refresh_theme(self) -> None:
        """بعد تغيير الثيم تُبنى الواجهة من جديد (main_window.rebuild_ui)؛ لا شيء هنا."""


# =====================================================================================================
# طبقة توافق: أسماء قريبة من ttk لنافذة الإعدادات (نفس pack/grid) لكن بمظهر Studio ومن رموز الثيم
# =====================================================================================================
def _bg_of(parent) -> str:
    try:
        return parent.cget("background")
    except (tk.TclError, ValueError):
        pass
    try:
        c = parent.cget("fg_color")
        return c if isinstance(c, str) else c[-1]
    except (tk.TclError, ValueError, AttributeError):
        return theme.tokens()["bg"]


def _plain(text: str) -> str:
    """نصوص الواجهة القديمة تصل مغلّفة بـ R() وبمسافات theme.ck؛ الودجات الجديدة تغلّف بنفسها."""
    return text.replace(theme.RLE, "").replace(theme.PDF, "").strip()


class Frame(tk.Frame):
    def __init__(self, parent, **kw):
        kw.pop("padding", None)
        super().__init__(parent, background=kw.pop("background", None) or _bg_of(parent), borderwidth=0,
                         highlightthickness=0, **kw)


_LABEL = {  # النمط: (الحجم، الوزن، رمز اللون)
    "TLabel": (14, "normal", "fg"), "Muted.TLabel": (13, "normal", "muted"), "Status.TLabel": (13, "normal", "muted"),
    "Ok.TLabel": (14, "bold", "ok"), "Warn.TLabel": (14, "normal", "warn"), "Err.TLabel": (14, "normal", "err"),
    "Subtitle.TLabel": (21, "bold", "fg"), "Title.TLabel": (26, "bold", "fg"),
}


def label_font(style: str) -> tuple:
    size, weight, _c = _LABEL.get(style, _LABEL["TLabel"])
    return _tkfont(size, weight)


class Label(tk.Label):
    def __init__(self, parent, text: str = "", style: str = "TLabel", **kw):
        kw.setdefault("borderwidth", 0)
        super().__init__(parent, text=text, background=_bg_of(parent), **kw)
        self.configure(style=style)

    def configure(self, cnf=None, **kw):
        style = kw.pop("style", None)
        if style is not None:
            size, weight, key = _LABEL.get(style, _LABEL["TLabel"])
            kw["font"] = _tkfont(size, weight)
            kw["foreground"] = theme.tokens()[key]
        return super().configure(cnf, **kw) if (kw or cnf) else None

    config = configure


class Para(Label):
    """فقرة عربية تلتف يدوياً (Tk يفقد اتجاه السطر الثاني)، وتعيد الالتفاف عند تغيير العرض."""

    def __init__(self, parent, text: str = "", style: str = "Muted.TLabel", **kw):
        super().__init__(parent, style=style, anchor="e", justify="right", **kw)
        self._text, self._width = text, 0
        self.bind("<Configure>", self._on_resize)
        self.set(text)

    def set(self, text: str) -> None:
        self._text = text
        self._render()

    def _on_resize(self, e) -> None:
        if abs(e.width - self._width) > 4:
            self._width = e.width
            self._render()

    def _render(self) -> None:
        width = self._width or px(600)
        try:
            tk.Label.configure(self, text=theme.wrap_rtl(self._text, self.cget("font"), max(width - px(8), px(80)))
                               if self._text else "")
        except tk.TclError:
            tk.Label.configure(self, text=R(self._text))


def link(parent, text: str, command, size: int = 14) -> tk.Label:
    t = theme.tokens()
    lbl = tk.Label(parent, text=R(text), foreground=t["accent_text"], background=_bg_of(parent), cursor="hand2",
                   font=(text_family(), -px(size * theme._text), "underline"), borderwidth=0, takefocus=1,
                   highlightthickness=1, highlightbackground=_bg_of(parent), highlightcolor=t["accent"])
    lbl.bind("<Button-1>", lambda e: command())
    lbl.bind("<Return>", lambda e: command())
    lbl.bind("<space>", lambda e: command())
    return lbl


def Button(parent, text: str = "", command=None, style: str = "TButton", state: str = "normal", width: int = 0,
           **_kw) -> Btn:
    b = Btn(parent, _plain(text), command, kind="primary" if style == "Accent.TButton" else "outline",
            height=36, min_width=max(width, 0) * 9 or 0)
    if state != "normal":
        b.configure(state=state)
    return b


def Check(parent, text: str = "", variable=None, command=None) -> CheckRow:
    return CheckRow(parent, _plain(text), variable, command or (lambda: None), _bg_of(parent))


class Entry(ctk.CTkEntry):
    def __init__(self, parent, textvariable=None, show: str = "", width: int = 0, **kw):
        t = theme.tokens()
        super().__init__(parent, textvariable=textvariable, show=show, height=36, corner_radius=8, border_width=1,
                         width=(width * 10 if width else 200), fg_color=t["raised"], border_color=t["strong"],
                         text_color=t["fg"], placeholder_text_color=t["muted"], font=font(14),
                         bg_color=_bg_of(parent))


class Combo(ctk.CTkOptionMenu):
    """قائمة منسدلة بواجهة Combobox: values / current(i) / bind("<<ComboboxSelected>>")."""

    def __init__(self, parent, textvariable: tk.StringVar, values=(), width: int = 0, state: str = "readonly", **_kw):
        t = theme.tokens()
        self._var = textvariable
        self._values = [str(v) for v in values]
        self._callbacks: list = []
        super().__init__(parent, variable=textvariable, values=self._values or [""], height=36, corner_radius=8,
                         width=(width * 12 + 40 if width else 260), fg_color=t["raised"], button_color=t["raised"],
                         button_hover_color=t["select"], text_color=t["fg"], dropdown_fg_color=t["card"],
                         dropdown_text_color=t["fg"], dropdown_hover_color=t["select"], font=font(14),
                         dropdown_font=font(14), bg_color=_bg_of(parent), anchor="w",
                         command=lambda v: [cb(None) for cb in self._callbacks])

    def configure(self, **kw):
        if "values" in kw:
            self._values = [str(v) for v in kw.pop("values")]
            kw["values"] = self._values or [""]
        return super().configure(**kw)

    def current(self, index: int | None = None):
        if index is None:
            v = self._var.get()
            return self._values.index(v) if v in self._values else -1
        self._var.set(self._values[index])
        self.set(self._values[index])
        return None

    def bind(self, sequence=None, command=None, add=None):
        if sequence == "<<ComboboxSelected>>":
            self._callbacks.append(command)
            return None
        return super().bind(sequence, command, add)


class Tabs(tk.Frame):
    """تبويبات علوية: frames[key] لكل تبويب، select(i) و current() بدل ttk.Notebook."""

    def __init__(self, parent, items: list[tuple[str, str]], make_frame, on_change=None):
        super().__init__(parent, background=_bg_of(parent), borderwidth=0, highlightthickness=0)
        self.keys = [k for k, _t in items]
        self.var = tk.StringVar(value=self.keys[0])
        self._on_change = on_change
        bar = tk.Frame(self, background=_bg_of(parent), borderwidth=0, highlightthickness=0)
        bar.pack(fill="x", pady=(0, px(10)))
        self.seg = Segmented(bar, [(k, t, "") for k, t in items], self.var)
        self.seg.pack(side="right")
        self.var.trace_add("write", lambda *_: self._changed())
        self.holder = tk.Frame(self, background=_bg_of(parent), borderwidth=0, highlightthickness=0)
        self.holder.pack(fill="both", expand=True)
        self.frames = {k: make_frame(self.holder) for k in self.keys}
        self._shown = None
        self._changed()

    def _changed(self) -> None:
        key = self.var.get()
        if self._shown:
            self.frames[self._shown].pack_forget()
        self.frames[key].pack(fill="both", expand=True)
        self._shown = key
        if self._on_change:
            self._on_change()

    def select(self, i: int) -> None:
        self.var.set(self.keys[i])

    def current(self) -> int:
        return self.keys.index(self.var.get())


class ResultsTable(tk.Frame):
    """جدول نتائج صغير للقراءة فقط: insert("", "end", values=…) و delete(*get_children())."""

    def __init__(self, parent, columns: list[tuple[str, str, int, bool]]):
        t = theme.tokens()
        super().__init__(parent, background=t["card"], borderwidth=0, highlightthickness=1,
                         highlightbackground=t["line"])
        self._cols = columns
        for i, (_cid, title, w, stretch) in enumerate(columns):
            self.columnconfigure(i, weight=1 if stretch else 0, minsize=px(w))
            tk.Label(self, text=R(title), background=t["card"], foreground=t["muted"], font=_tkfont(12, "bold"),
                     anchor="e", borderwidth=0).grid(row=0, column=i, sticky="ew", padx=px(8), pady=px(6))
        tk.Frame(self, background=t["line"], height=1).grid(row=1, column=0, columnspan=len(columns), sticky="ew")
        self._n = 2

    def get_children(self):
        return ()

    def delete(self, *_keys) -> None:
        for w in self.grid_slaves():
            if int(w.grid_info()["row"]) >= 2:
                w.destroy()
        self._n = 2

    def insert(self, _parent, _where, values=()) -> None:
        t = theme.tokens()
        for i, v in enumerate(values):
            tk.Label(self, text=R(str(v)), background=t["card"], foreground=t["fg"], font=_tkfont(13), anchor="e",
                     borderwidth=0).grid(row=self._n, column=i, sticky="ew", padx=px(8), pady=px(4))
        self._n += 1


class PopupMenu:
    """قائمة منبثقة بشكل البرنامج (بدل قائمة ويندوز الأصلية): نفس ألوان البطاقات والخطوط والحواف، وصفوف
    بارتفاع مريح تتلوّن عند المرور، بمحاذاة يمين. نفس واجهة tk.Menu المستخدمة: add_command وadd_separator
    وindex("end") وtk_popup."""

    def __init__(self, master):
        self.master = master
        self.items: list[tuple[str, object]] = []
        self.win: tk.Toplevel | None = None

    def add_command(self, label: str, command) -> None:
        self.items.append((label, command))

    def add_separator(self) -> None:
        self.items.append(("", None))

    def index(self, _what: str = "end"):
        return len(self.items) - 1 if self.items else None

    def tk_popup(self, x: int, y: int) -> None:
        t = theme.tokens()
        w = self.win = tk.Toplevel(self.master)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(background=t["line"])
        body = tk.Frame(w, background=t["card"], borderwidth=0, highlightthickness=0)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        inner = tk.Frame(body, background=t["card"])
        inner.pack(fill="both", expand=True, pady=px(6))
        for label, cmd in self.items:
            if cmd is None:
                tk.Frame(inner, background=t["line"], height=1).pack(fill="x", padx=px(10), pady=px(4))
                continue
            row = tk.Label(inner, text=label, font=_tkfont(14), foreground=t["fg"], background=t["card"],
                           anchor="e", justify="right", padx=px(18), pady=px(8), cursor="hand2", borderwidth=0)
            row.pack(fill="x", padx=px(6))
            row.bind("<Enter>", lambda e, r=row: r.configure(background=t["select"]))
            row.bind("<Leave>", lambda e, r=row: r.configure(background=t["card"]))
            row.bind("<ButtonRelease-1>", lambda e, c=cmd: self._run(c))
        w.update_idletasks()
        mw, mh = w.winfo_reqwidth(), w.winfo_reqheight()
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        left = x - mw if x - mw >= 0 else x          # واجهة عربية: القائمة تمتد لليسار من المؤشر
        top = y if y + mh <= sh - px(40) else max(0, y - mh)
        w.geometry(f"+{min(left, sw - mw)}+{top}")
        w.bind("<Escape>", lambda e: self.close())
        w.bind("<FocusOut>", lambda e: self.close())
        w.focus_force()
        self._outside = self.master.winfo_toplevel().bind("<Button-1>", lambda e: self.close(), add="+")

    def _run(self, cmd) -> None:
        self.close()
        cmd()

    def close(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.win.destroy()
        self.win = None
        try:
            self.master.winfo_toplevel().unbind("<Button-1>", self._outside)
        except (tk.TclError, AttributeError):
            pass
