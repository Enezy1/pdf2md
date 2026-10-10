"""مظهر ويندوز 11 (sv-ttk = ثيم Fluent الرسمي لـ ttk) + الخطوط والمسافات واتجاه النص.

- الوضع الفاتح/الداكن يتبع النظام (darkdetect) ما لم يُختر غير ذلك في "متقدم".
- كل نص عربي يُمرَّر عبر R() (تضمين RLE) حتى يُرسم من اليمين لليسار في Tk.
- المقاسات بالبكسل تُضرب في مقياس الشاشة (px) حتى لا تضيق على الشاشات المكبّرة.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from . import themes

RLE, PDF = "‫", "‬"  # تضمين من اليمين لليسار … نهاية التضمين
FONT = "Segoe UI"

# مسافات متسقة (قبل ضربها في مقياس الشاشة)
S1, S2, S3, S4 = 4, 8, 12, 16

# Tk على ويندوز يقطع رسم السطر العربي الطويل (نحو 100 حرف) إلى أجزاء بترتيب خاطئ؛ نلف قبل ذلك.
MAX_LINE = 80


def R(s: str) -> str:
    """Tk يرسم كل سطر باتجاه أساسي من اليسار لليمين. تضمين النص بين RLE و PDF يجعل اتجاهه
    الأساسي من اليمين لليسار، فتبقى علامات الترقيم في مكانها، ويبقى ترتيب الكلمات صحيحاً حين
    يختلط العربي بكلمات لاتينية (OCR، PDF، DPI) أو بامتداد اسم الملف (.pdf).
    (علامة RLM وحدها لا تكفي: تصلح الترقيم في الأطراف لكنها لا تغيّر الاتجاه الأساسي.)"""
    return f"{RLE}{s}{PDF}"


_scale = 1.0
_text = 1.0  # إعداد "حجم النص" في ويندوز (إمكانية الوصول)، مستقل عن تكبير الشاشة
_dark = False

# أحجام الخطوط بالنقاط قبل ضربها في _text. النص الأساسي 11: الحرف العربي بنقاطه وحركاته
# يحتاج أكبر من 10 ليُقرأ براحة. الثانوي (رمادي) أصغر بدرجة حتى يتميّز بالحجم لا باللون وحده.
BODY, SMALL, STRONG, SUBTITLE, TITLE = 11, 10, 12, 13, 16


def fs(points: int) -> int:
    """حجم خط بالنقاط بعد تطبيق "حجم النص" في ويندوز."""
    return int(round(points * _text))


def text_scale() -> float:
    """يقرأ "حجم النص" من إعدادات ويندوز (100–225%)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Accessibility") as k:
            v = winreg.QueryValueEx(k, "TextScaleFactor")[0]
        return min(max(int(v), 100), 225) / 100
    except OSError:
        return 1.0


def px(n: float) -> int:
    return int(round(n * _scale))


def is_dark() -> bool:
    return _dark


def system_theme() -> str:
    try:
        import darkdetect
        return "dark" if darkdetect.isDark() else "light"
    except Exception:
        return "light"


_pname = themes.DEFAULT


def palette_name() -> str:
    return _pname


def tokens() -> dict[str, str]:
    """رموز الألوان الحالية (themes.py) للثيم والوضع المطبّقين."""
    return themes.tokens(_pname, _dark)


def palette() -> dict[str, str]:
    """ألوان للعناصر غير ttk (Text, Canvas, Listbox) ولنصوص الحالة."""
    t = tokens()
    return {"bg": t["bg"], "card": t["card"], "fg": t["fg"], "muted": t["muted"], "border": t["strong"],
            "accent": t["accent_text"], "warn": t["warn"], "ok": t["ok"], "err": t["err"],
            "accent_fill": t["accent"], "on_accent": t["on_accent"], "accent2": t["accent2"],
            "select": t["select"], "deep": t["deep"], "line": t["line"], "raised": t["raised"]}


def apply(root: tk.Tk, mode: str = "system", palette_pick: str = "") -> None:
    """mode: "system" أو "light" أو "dark". palette_pick: اسم ثيم الألوان من themes.THEMES ("" = الأصلي)."""
    global _scale, _dark, _text, _pname
    _scale = max(root.winfo_fpixels("1i") / 96.0, 1.0)
    _text = text_scale()
    want = system_theme() if mode not in ("light", "dark") else mode
    _dark = want == "dark"
    _pname = themes.resolve(palette_pick)
    try:
        import sv_ttk
        sv_ttk.set_theme(want, root)
        _recolor(root, want)
        # ألوان sv-ttk تُطبَّق عادة عند وصول حدث <<ThemeChanged>>؛ نطبّقها فوراً حتى تقرأها الواجهة الآن
        root.tk.eval("configure_colors")
    except Exception:  # بدون sv-ttk: الثيم الافتراضي يبقى صالحاً
        pass
    _fonts(root)
    _styles()
    root.configure(background=background())
    try:  # شريط العنوان الداكن في ويندوز 11
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        val = ctypes.c_int(1 if _dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass


# ---------------------------------------------------------------- إعادة تلوين sv-ttk
# sv-ttk يرسم الأزرار والمربعات والمؤشر وشريط التقدّم من صور (sprites) زرقاء ورمادية ثابتة.
# نعيد تلوين الصور من رموز themes.py: الأزرق ← accent، والرمادي ← سلّم الخلفيات المصبوغة.
_ORIG = {  # ألوان sv-ttk الأصلية: خلفية، قاعدة الأزرق في الصور
    "dark": {"accent": "#57c8ff", "ramp": ((0x00, "deep"), (0x1c, "bg"), (0x2b, "card"), (0x2f, "raised"),
                                           (0x40, "line"), (0x80, "strong"), (0xff, "fg"))},
    "light": {"accent": "#005fb8", "ramp": ((0x00, "fg"), (0x8a, "strong"), (0xc4, "line"), (0xe7, "deep"),
                                            (0xfa, "bg"), (0xff, "raised"))},
}


def _rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def _recolored_sheet(mode: str, t: dict[str, str], dst) -> None:
    import colorsys
    from pathlib import Path

    import numpy as np
    import sv_ttk
    from PIL import Image

    src = Path(sv_ttk.__file__).parent / "theme" / f"spritesheet_{mode}.png"
    img = np.asarray(Image.open(src).convert("RGBA")).astype(np.float64)
    rgb, alpha = img[..., :3], img[..., 3:]
    mx, mn = rgb.max(-1), rgb.min(-1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0.0)
    # الرمادي ← سلّم الألوان المصبوغة (استيفاء خطي بين نقاط مرجعية)
    ramp = _ORIG[mode]["ramp"]
    xs = [a for a, _ in ramp]
    gray = rgb.mean(-1)
    out = np.stack([np.interp(gray, xs, [_rgb(t[k])[c] for _, k in ramp]) for c in range(3)], -1)
    # الأزرق ← accent: نحافظ على نسب التشبّع والسطوع حتى تبقى حالات المرور والضغط متمايزة
    hb, sb, vb = colorsys.rgb_to_hsv(*(v / 255 for v in _rgb(_ORIG[mode]["accent"])))
    ha, sa, va = colorsys.rgb_to_hsv(*(v / 255 for v in _rgb(t["accent"])))
    uniq, inv = np.unique(rgb.reshape(-1, 3), axis=0, return_inverse=True)
    mapped = np.empty_like(uniq)
    for i, (r, g, b) in enumerate(uniq):
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        mapped[i] = [c * 255 for c in colorsys.hsv_to_rgb(ha, min(1.0, s * sa / sb), min(1.0, v * va / vb))]
    acc = mapped[inv.reshape(-1)].reshape(rgb.shape)
    w = np.clip((sat - 0.06) / 0.18, 0, 1)[..., None]
    final = np.clip(acc * w + out * (1 - w), 0, 255)
    Image.fromarray(np.concatenate([final, alpha], -1).astype(np.uint8), "RGBA").save(dst)


def _recolor(root: tk.Tk, mode: str) -> None:
    """يطبّق رموز الثيم على صور sv-ttk وعلى مصفوفة colors في Tcl. الثيم الأصلي يعيد الصور كما شُحنت."""
    import tempfile
    from pathlib import Path

    import sv_ttk

    ns = f"ttk::theme::sv_{mode}"
    tk_ = root.tk
    t = tokens()
    sheet = Path(sv_ttk.__file__).parent / "theme" / f"spritesheet_{mode}.png"
    classic = _pname == "classic"
    if not classic:
        sheet = Path(tempfile.gettempdir()) / f"pdf2md-sprites-{_pname}-{mode}.png"
        _recolored_sheet(mode, t, sheet)
    names = tk_.splitlist(tk_.eval("set ::spriteinfo"))
    tk_.call("image", "create", "photo", "_pdf2md_sheet", "-file", str(sheet))
    for i in range(0, len(names), 5):
        name, x, y, w, h = names[i:i + 5]
        img = tk_.eval(f"set {ns}::I({name})")
        tk_.call(img, "copy", "_pdf2md_sheet", "-from", x, y, int(x) + int(w), int(y) + int(h),
                 "-compositingrule", "set")
    tk_.call("image", "delete", "_pdf2md_sheet")
    if classic:
        return
    for k, v in (("-bg", t["bg"]), ("-fg", t["fg"]), ("-selbg", t["accent"]), ("-selfg", t["on_accent"]),
                 ("-accent", t["accent"]), ("-disfg", t["disabled"])):
        tk_.call("set", f"{ns}::colors({k})", v)


def _mix(a: str, b: str, f: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * f) for x, y in zip(ra, rb))


class Gradient(tk.Canvas):
    """شريط تدرّج ناعم (أفقي أو رأسي) من رموز الثيم. يُعاد رسمه عند تغيير الحجم.
    stops: أسماء رموز من themes (مثل "accent", "accent2") أو ألوان جاهزة من tokens()."""

    def __init__(self, parent, height: int = 3, stops=("accent", "accent2"), vertical: bool = False, **kw):
        super().__init__(parent, height=px(height), highlightthickness=0, borderwidth=0, **kw)
        self._stops, self._vertical, self._size = stops, vertical, (0, 0)
        self.bind("<Configure>", lambda e: self._paint(e.width, e.height))

    def _paint(self, w: int, h: int) -> None:
        t = tokens()
        cols = [t[s] for s in self._stops]
        n = h if self._vertical else w
        if n < 2:
            return
        self.delete("all")
        step = 2
        for i in range(0, n, step):
            f = i / (n - 1) * (len(cols) - 1)
            k = min(int(f), len(cols) - 2)
            c = _mix(cols[k], cols[k + 1], f - k)
            if self._vertical:
                self.create_rectangle(0, i, w, i + step, fill=c, outline=c)
            else:
                self.create_rectangle(i, 0, i + step, h, fill=c, outline=c)


class GradientButton(tk.Canvas):
    """زر رئيسي بتدرّج أفقي هادئ (accent إلى accent2) كما في تصميم Studio؛ يقبل configure(text, style, state)
    مثل ttk.Button. style="TButton" يعني زراً عادياً مسطّحاً (حالة "إيقاف")."""

    def __init__(self, parent, text: str, command, width: int = 190, height: int = 42):
        super().__init__(parent, width=px(width), height=px(height), highlightthickness=0, borderwidth=0,
                         cursor="hand2", takefocus=1)
        self._text, self._cmd, self._state, self._flat, self._hover = text, command, "normal", False, False
        self._bw, self._bh = px(width), px(height)
        self.bind("<Configure>", lambda e: self._set_size(e.width, e.height))
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", lambda e: self._click())
        self.bind("<Return>", lambda e: self._click())
        self.bind("<space>", lambda e: self._click())
        self.bind("<FocusIn>", lambda e: self._paint())
        self.bind("<FocusOut>", lambda e: self._paint())
        self._paint()

    def _set_size(self, w: int, h: int) -> None:
        self._bw, self._bh = w, h
        self._paint()

    def _set_hover(self, v: bool) -> None:
        self._hover = v
        self._paint()

    def _click(self) -> None:
        if self._state == "normal":
            self._cmd()

    def configure(self, cnf=None, **kw):
        if "text" in kw:
            self._text = kw.pop("text")
        if "state" in kw:
            self._state = kw.pop("state")
        if "style" in kw:
            self._flat = kw.pop("style") == "TButton"
        if not kw and not cnf:
            self._paint()
            return None
        return super().configure(cnf, **kw)

    config = configure

    def _paint(self) -> None:
        t = tokens()
        self.delete("all")
        w, h = max(self._bw, 4), max(self._bh, 4)
        tk.Canvas.configure(self, background=background())
        off = self._state != "normal"
        flat = self._flat or off
        if flat:
            fg = t["disabled"] if off else t["fg"]
            stops = (t["raised"], t["raised"])
        else:
            a, b = t["accent"], t["accent2"]
            if self._hover:
                a, b = _mix(a, "#ffffff", 0.08), _mix(b, "#ffffff", 0.08)
            stops, fg = (a, b), t["on_accent"]
        self._img = _round_gradient(w, h, stops, px(6), t["line"] if flat else None)
        self.create_image(0, 0, image=self._img, anchor="nw")
        if self.focus_displayof() is self:
            self.create_rectangle(4, 4, w - 5, h - 5, outline=t["accent"] if flat else t["on_accent"], dash=(2, 2))
        self.create_text(w // 2, h // 2, text=self._text, fill=fg, font=(FONT, fs(STRONG), "bold"))


def _round_gradient(w: int, h: int, stops: tuple[str, str], radius: int, border: str | None):
    """صورة مستطيل بزوايا مدوّرة منعّمة وتدرّج أفقي (وحدّ اختياري)، بحجم الزر بالضبط."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageTk

    k = 4  # رسم بدقة أعلى ثم تصغير = حواف ناعمة
    mask = Image.new("L", (w * k, h * k), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * k - 1, h * k - 1), radius * k, fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)
    a, b = np.array(_rgb(stops[0]), float), np.array(_rgb(stops[1]), float)
    f = np.linspace(0, 1, w)[None, :, None]
    body = np.broadcast_to(a + (b - a) * f, (h, w, 3)).astype(np.uint8)
    img = Image.fromarray(body, "RGB").convert("RGBA")
    if border:
        edge = Image.new("RGBA", (w, h), border)
        inner = Image.new("L", (w * k, h * k), 0)
        ImageDraw.Draw(inner).rounded_rectangle((k, k, w * k - 1 - k, h * k - 1 - k), max(radius - 1, 1) * k, fill=255)
        edge.paste(img, (0, 0), inner.resize((w, h), Image.LANCZOS))
        img = edge
    img.putalpha(mask)
    return ImageTk.PhotoImage(img)


def tree_tags(tree: ttk.Treeview) -> None:
    """حالة الصف: المحدَّد للمعالجة بنص أساسي، وغير المحدَّد خافت."""
    t = tokens()
    tree.tag_configure("checked", foreground=t["fg"])
    tree.tag_configure("unchecked", foreground=t["muted"])
    tree.tag_configure("problem", foreground=t["err"])


def dark_titlebar(win: tk.Toplevel) -> None:
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1 if _dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass


def background() -> str:
    st = ttk.Style()
    return str(st.lookup(".", "background") or st.lookup("TFrame", "background") or palette()["bg"])


def _fonts(root: tk.Tk) -> None:
    for name, size, weight in (
        ("TkDefaultFont", BODY, "normal"), ("TkTextFont", BODY, "normal"), ("TkMenuFont", BODY, "normal"),
        ("TkHeadingFont", BODY, "bold"), ("TkCaptionFont", SMALL, "normal"), ("TkTooltipFont", SMALL, "normal"),
        ("SunValleyBodyFont", BODY, "normal"), ("SunValleyCaptionFont", SMALL, "normal"),
        ("SunValleyBodyStrongFont", BODY, "bold"), ("SunValleySubtitleFont", SUBTITLE, "bold"),
    ):
        try:
            tkfont.nametofont(name).configure(family=FONT, size=fs(size), weight=weight)
        except tk.TclError:
            pass


def _styles() -> None:
    c = palette()
    st = ttk.Style()
    t = tokens()
    st.configure("Title.TLabel", font=(FONT, fs(TITLE), "bold"))
    st.configure("Subtitle.TLabel", font=(FONT, fs(SUBTITLE), "bold"))
    st.configure("Muted.TLabel", foreground=c["muted"], font=(FONT, fs(SMALL)))
    st.configure("Status.TLabel", foreground=c["muted"])
    st.configure("CardStatus.TLabel", foreground=c["muted"], background=c["card"])
    st.configure("CardWarn.TLabel", foreground=c["warn"], background=c["card"])
    st.configure("Ok.TLabel", foreground=c["ok"], font=(FONT, fs(STRONG), "bold"))
    st.configure("Warn.TLabel", foreground=c["warn"])
    st.configure("Err.TLabel", foreground=c["err"])
    st.configure("Link.TLabel", foreground=c["accent"], font=(FONT, fs(BODY), "underline"))
    # الرابط حين يأخذ التركيز من لوحة المفاتيح: خط عريض حتى يُرى أين المؤشر
    st.configure("LinkFocus.TLabel", foreground=c["accent"], font=(FONT, fs(BODY), "bold underline"))
    st.configure("Info.TLabel", foreground=c["accent"], font=(FONT, fs(BODY)))
    # ارتفاع الصف من قياس الخط نفسه حتى لا تتلاصق الأسطر حين يكبر النص
    line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
    st.configure("Treeview", rowheight=max(px(30), int(line * 1.8)))
    st.configure("Treeview.Heading", font=(FONT, fs(BODY)))
    st.configure("TNotebook.Tab", padding=(px(S4), px(S2)))
    if _pname != "classic":
        # الزر الرئيسي ("ابدأ") والأزرار المبدّلة: نص مقروء فوق لون التمييز في كل ثيم
        st.configure("Accent.TButton", foreground=t["on_accent"])
        st.map("Accent.TButton", foreground=[("disabled", t["disabled"]), ("pressed", t["on_accent"])])
        st.map("Toggle.TButton", foreground=[("selected disabled", t["disabled"]), ("disabled", t["disabled"]),
                                            ("selected", t["on_accent"]), ("pressed", t["fg"])])
        st.map("Treeview", background=[("selected", t["select"])], foreground=[("selected", t["fg"])])
        st.configure("Treeview", background=t["card"], fieldbackground=t["card"], foreground=t["fg"])
    st.configure("Card.TFrame", background=t["card"])
    # مربع الاختيار على يمين النص (واجهة من اليمين لليسار)
    try:
        st.layout("TCheckbutton", _mirror(st.layout("TCheckbutton")))
    except tk.TclError:
        pass


def _mirror(layout):
    out = []
    for name, opts in layout:
        opts = dict(opts)
        if opts.get("side") == "left":
            opts["side"] = "right"
        elif opts.get("side") == "right":
            opts["side"] = "left"
        if "children" in opts:
            opts["children"] = _mirror(opts["children"])
        out.append((name, opts))
    return out


def ck(text: str) -> str:
    """نص مربع اختيار: مسافة بين المربع (على اليمين) والنص."""
    return R("  " + text)


def split_long(text: str, limit: int = MAX_LINE) -> list[str]:
    """يقسم السطر الطويل عند الكلمات حتى لا يتجاوز limit حرفاً (انظر MAX_LINE)."""
    out, cur = [], ""
    for word in text.split(" "):
        trial = f"{cur} {word}" if cur else word
        if cur and len(trial) > limit:
            out.append(cur)
            cur = word
        else:
            cur = trial
    out.append(cur)
    return out


def wrap_rtl(text: str, font, width: int) -> str:
    """التفاف يدوي للنص العربي: Tk يلف السطر ثم يرسم كل سطر وحده، فيفقد السطر الثاني اتجاهه.
    نقسّم الكلمات بأنفسنا ونضمّن كل سطر في R() على حدة."""
    f = tkfont.Font(font=font) if not isinstance(font, tkfont.Font) else font
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}" if cur else word
        if cur and (f.measure(trial) > width or len(trial) > MAX_LINE):
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return "\n".join(R(x) for x in lines)


class Para(ttk.Label):
    """فقرة عربية تلتف يدوياً وتعيد الالتفاف عند تغيير حجم النافذة."""

    def __init__(self, parent, text: str = "", style: str = "TLabel", **kw):
        super().__init__(parent, style=style, anchor="e", justify="right", **kw)
        self._text = text
        self._width = 0
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
        font = self.cget("font") or ttk.Style().lookup(self.cget("style") or "TLabel", "font") or "TkDefaultFont"
        try:
            self.configure(text=wrap_rtl(self._text, font, max(width - px(S2), px(80))) if self._text else "")
        except tk.TclError:
            self.configure(text=R(self._text))


def link(parent, text: str, command) -> ttk.Label:
    """رابط نصي يعمل بالفأرة وبلوحة المفاتيح (Tab ثم Enter أو مسافة)."""
    lbl = ttk.Label(parent, text=R(text), style="Link.TLabel", cursor="hand2", takefocus=1)
    lbl.bind("<Button-1>", lambda _e: command())
    lbl.bind("<Return>", lambda _e: command())
    lbl.bind("<space>", lambda _e: command())
    lbl.bind("<FocusIn>", lambda _e: lbl.configure(style="LinkFocus.TLabel"))
    lbl.bind("<FocusOut>", lambda _e: lbl.configure(style="Link.TLabel"))
    return lbl


def text_widget(parent, height: int = 6) -> tk.Text:
    c = palette()
    t = tk.Text(parent, height=height, wrap="word", state="disabled", font=(FONT, fs(SMALL)), relief="flat",
                background=c["card"], foreground=c["fg"], insertbackground=c["fg"], borderwidth=0,
                highlightthickness=1, highlightbackground=c["border"], highlightcolor=c["accent"],
                padx=px(S2), pady=px(S2))
    t.tag_configure("rtl", justify="right")
    return t


def ltr(s: str) -> str:
    """مسار ملف: يُعرض من اليسار لليمين (LRE) حتى لا تنقلب أجزاؤه العربية."""
    return "\u202A" + s + "\u202C"


def shorten_path(path: str, font, width: int) -> str:
    """يختصر المسار من وسطه ليبقى أوله (القرص) وآخره (اسم المجلد) ظاهرين:
    C:\\Users\\…\\Reports\\_markdown."""
    f = tkfont.Font(font=font) if not isinstance(font, tkfont.Font) else font
    if f.measure(path) <= width:
        return path
    sep = "\\" if "\\" in path else "/"
    parts = path.split(sep)
    head, tail = parts[:1], parts[1:]
    while len(tail) > 1:
        tail = tail[1:]
        trial = sep.join(head + ["…"] + tail)
        if f.measure(trial) <= width:
            return trial
    last = tail[-1] if tail else path
    while len(last) > 4 and f.measure("…" + last) > width:
        last = last[1:]
    return "…" + last


class PathLabel(ttk.Label):
    """مسار يُختصر من الوسط حسب العرض المتاح، ويُعاد اختصاره عند تغيير حجم النافذة.
    suffix: نص عربي يُضاف بعد المسار (مثل "(افتراضي)")."""

    def __init__(self, parent, style: str = "Muted.TLabel", **kw):
        super().__init__(parent, style=style, anchor="e", **kw)
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
        font = ttk.Style().lookup(self.cget("style") or "TLabel", "font") or "TkDefaultFont"
        f = tkfont.Font(font=font)
        extra = f.measure(self._suffix + "   ") if self._suffix else 0
        width = max((self._width or px(400)) - extra - px(S2), px(80))
        text = ltr(shorten_path(self._path, f, width)) if self._path else ""
        if self._suffix:
            text = f"{text}   {R(self._suffix)}"
        try:
            self.configure(text=text)
        except tk.TclError:
            pass


def work_area(win: tk.Misc) -> tuple[int, int, int, int]:
    """منطقة العمل في الشاشة (x, y, عرض, ارتفاع) بدون شريط المهام. بديلها حجم الشاشة كاملاً."""
    try:
        import ctypes
        import ctypes.wintypes as wt
        r = wt.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(r), 0):  # SPI_GETWORKAREA
            return r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:
        pass
    return 0, 0, win.winfo_screenwidth(), win.winfo_screenheight() - px(48)


def fit_window(win: tk.Misc, w: int, h: int, min_w: int, min_h: int) -> None:
    """حجم النافذة وموضعها داخل منطقة العمل (فوق شريط المهام دائماً، بما فيه إطار النافذة وشريط عنوانها)،
    فلا يهبط أي جزء منها تحت الشريط حتى على لابتوب 1366×768 بتكبير 125% أو 150%."""
    # النص المكبّر في ويندوز يحتاج نافذة أوسع بالنسبة نفسها، ثم يُقصّ الناتج على الشاشة
    w, h, min_w, min_h = (int(v * _text) for v in (w, h, min_w, min_h))
    ax, ay, aw, ah = work_area(win)
    frame = px(48)  # شريط العنوان وحدود النافذة
    max_w, max_h = aw - px(16), ah - frame - px(8)
    w, h = min(w, max_w), min(h, max_h)
    win.minsize(min(min_w, max_w), min(min_h, max_h))
    win.maxsize(aw, ah)
    x = ax + (aw - w) // 2
    y = ay + max((ah - h - frame) // 2, 0)
    win.geometry(f"{w}x{h}+{x}+{y}")


def bind_key(win: tk.Misc, vk: int, fn, shift: bool = False) -> None:
    """اختصار Ctrl+حرف يعمل مهما كانت لغة لوحة المفاتيح: نطابق رمز الزر الفعلي (keycode)
    لا الحرف، لأن Ctrl+O مع اللوحة العربية يصل حرفاً عربياً (انظر clipboard.py)."""
    def handler(e):
        if e.keycode != vk or bool(e.state & 0x1) != shift:
            return None
        fn()
        return "break"
    win.bind("<Control-KeyPress>", handler, add="+")


def friendly_error(e: BaseException) -> str:
    """رسالة خطأ للمستخدم: ما الذي حدث وماذا يفعل. التفاصيل التقنية تبقى في السجل."""
    if isinstance(e, PermissionError):
        return "لا توجد صلاحية للوصول إلى الملف أو المجلد.\nأغلق أي برنامج يستخدمه، أو اختر مكاناً آخر."
    if isinstance(e, FileNotFoundError):
        return "الملف أو المجلد غير موجود.\nربما نُقل أو حُذف؛ اختره من جديد."
    if isinstance(e, (ConnectionError, TimeoutError)) or type(e).__name__ in (
            "ConnectError", "ConnectTimeout", "ReadTimeout", "HTTPError", "URLError"):
        return "تعذّر الاتصال بالإنترنت أو بالخادم.\nتحقق من الاتصال ثم حاول مرة أخرى."
    if isinstance(e, OSError) and getattr(e, "errno", None) == 28:
        return "لا توجد مساحة كافية على القرص.\nحرّر مساحة ثم حاول مرة أخرى."
    if isinstance(e, OSError):
        return "تعذّرت قراءة القرص أو الكتابة عليه.\nتأكد أن المسار متاح ثم حاول مرة أخرى."
    msg = str(e).strip()
    # رسائل ValueError العربية في هذا البرنامج مكتوبة للمستخدم أصلاً
    if isinstance(e, ValueError) and any("؀" <= ch <= "ۿ" for ch in msg):
        return msg
    return "حدث خطأ غير متوقع.\nالتفاصيل في سجل البرنامج (متقدم ← تصدير معلومات التشخيص)."
