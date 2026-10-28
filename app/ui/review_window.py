"""نافذة المراجعة في آخر الدفعة: لكل صفحة ضعيفة (وضع "عبر المواقع")
"أرسل لمزوّد" أو "اتركها [غير مقروء]"، مع إمكانية تحديد قصاصة من الصفحة بالسحب."""

from __future__ import annotations

import base64
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import pymupdf as fitz

from ..quality import WEAK_REASONS
from . import theme
from .theme import R, S1, S2, S3, px

PREVIEW_DPI = 60


class ReviewWindow:
    """items: [{"path","hash","page","reason","index"}]. on_apply(decisions) حيث كل قرار:
    {..item, "action": "send"|"leave"|"accept"|"later", "text", "clip": (x0,y0,x1,y1) بنقاط PDF أو None}."""

    def __init__(self, parent: tk.Misc, items: list[dict], on_apply, reader=None, engines=None):
        """reader(item) -> str اختياري: يقرأ الصفحة بنموذج رؤية (يُستدعى في خيط) ويرفع استثناءً عند الفشل."""
        self.items = items
        self.reader = reader
        self.engines = engines or [("auto", "تلقائي")]
        self.on_apply = on_apply
        self.actions = [tk.StringVar(value="later") for _ in items]
        self.clips: list[tuple | None] = [None] * len(items)
        self.texts = [it.get("text", "") for it in items]
        self.cur = -1
        self.win = tk.Toplevel(parent)
        self.win.title(R("مراجعة الصفحات الضعيفة"))
        theme.fit_window(self.win, px(980), px(720), px(640), px(480))
        theme.dark_titlebar(self.win)

        theme.Para(self.win, text=(
            "هذه صفحات شكّ الفحص الآلي في قراءتها، وأغلبها قُرئ فعلاً. الأسهل: اضغط «اقرأ الكل تلقائياً واحفظ» "
            "فيقرأ البرنامج كل صفحة من جديد (بنموذج أقوى) ويحفظ النص ويُخفيها من القائمة. "
            "وللصفحة الواحدة: «اقرأها الآن» تعرض النص لتعدّله ثم «اعتمد»، أو «تجاهل» لتركها بلا نص. "
            "ما اعتمدته يُحفظ عند إغلاق النافذة.")).pack(
            fill="x", padx=px(S2), pady=px(S2))

        # شريط الأزرار أولاً بـ side=bottom حتى لا تقصّه الصورة والقائمة عند صغر النافذة
        bar = ttk.Frame(self.win)
        bar.pack(side="bottom", fill="x", padx=px(S2), pady=px(S2))
        bar2 = ttk.Frame(self.win)
        bar2.pack(side="bottom", fill="x", padx=px(S2))

        main = ttk.Frame(self.win)
        main.pack(fill="both", expand=True, padx=px(S2))
        self.tree = ttk.Treeview(main, columns=("action", "reason", "page", "file"), show="headings")
        for c, t, w in (("action", "القرار", 110), ("reason", "السبب", 220), ("page", "صفحة", 50), ("file", "الملف", 200)):
            self.tree.heading(c, text=R(t), anchor="e")
            self.tree.column(c, width=px(w), anchor="e")
        for i, it in enumerate(items):
            self.tree.insert("", "end", iid=str(i))
            self._refresh(i)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._select())
        self.tree.bind("<Return>", lambda e: self._read_now())
        self.tree.bind("<Delete>", lambda e: self._set("leave"))
        self.win.bind("<Escape>", lambda e: self._close())
        self.tree.pack(side="right", fill="y")

        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(right, bg=theme.palette()["card"], highlightthickness=0, width=px(420), height=px(260))
        self.canvas.pack(fill="both", expand=True)
        self.text = theme.text_widget(right, height=7)
        self.text.configure(state="normal", undo=True)
        self.text.pack(fill="x", pady=(px(S1), 0))
        self.text.bind("<<Modified>>", self._text_changed)
        self.status = ttk.Label(right, text="", anchor="e")
        self.status.pack(fill="x")
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.rect = None
        self.scale = 1.0

        if reader:
            # طريقة القراءة: تلقائي (Gemini) أو مزوّد آخر أو OCR (Google Vision…) أو ذكاء/OCR محلي، لصفحات يرفضها Gemini
            self.engine_var = tk.StringVar(value=R(self.engines[0][1]))
            box = ttk.Combobox(bar2, textvariable=self.engine_var, state="readonly", width=40,
                               values=[R(lbl) for _k, lbl in self.engines], justify="right")
            box.pack(side="left")
            ttk.Label(bar2, text=R("طريقة القراءة:")).pack(side="left", padx=px(S1))
            self.read_btn = ttk.Button(bar2, text=R("اقرأها الآن وأرِني النص"), command=self._read_now)
            self.read_btn.pack(side="right", padx=(0, px(S3)))
        ttk.Button(bar2, text=R("اعتمد النص الظاهر"), command=self._accept).pack(side="right")
        ttk.Button(bar2, text=R("تجاهل (غير مقروء)"), command=lambda: self._set("leave")).pack(side="right", padx=px(S1))
        if reader:
            self.all_btn = ttk.Button(bar, text=R("اقرأ الكل تلقائياً واحفظ"), style="Accent.TButton",
                                      command=self._toggle_read_all)
            self.all_btn.pack(side="right")
        ttk.Button(bar, text=R("إغلاق (يحفظ ما اعتمدته)"), command=self._close).pack(side="left")
        self.win.protocol("WM_DELETE_WINDOW", self._close)
        self.canvas.bind("<Button-3>", lambda e: self._clear_clip())   # زر الفأرة الأيمن يلغي القصاصة المحددة
        self._all_stop = None
        if items:
            self.tree.selection_set("0")
            self.tree.focus("0")
            self.tree.focus_set()

    def _refresh(self, i: int):
        it = self.items[i]
        a = {"send": "أعد القراءة", "leave": "تجاهل", "accept": "✓ اعتُمد", "later": "— لم تقرر"}[self.actions[i].get()]
        if self.clips[i] and self.actions[i].get() == "send":
            a += " (قصاصة)"
        self.tree.item(str(i), values=(R(a), R(WEAK_REASONS.get(it["reason"], it["reason"])), it["page"],
                                       R(Path(it["path"]).name)))

    def _select(self):
        # الأسهم تُطلق حدث اختيار لكل صف: نؤجّل الرسم 120ms حتى يستقر الاختيار (كان كل ضغطة تفتح الـ PDF وترسم فتتعلق)
        self._sel_job = getattr(self, "_sel_job", None)
        if self._sel_job:
            self.win.after_cancel(self._sel_job)
        self._sel_job = self.win.after(120, self._select_now)

    def _preview(self, path: str, page: int):
        cache = self.__dict__.setdefault("_prev_cache", {})
        key = (path, page)
        if key not in cache:
            docs = self.__dict__.setdefault("_docs", {})
            if path not in docs:
                docs[path] = fitz.open(path)     # يبقى مفتوحاً: فتح ملف 400 صفحة في كل ضغطة هو البطء
            pix = docs[path][page - 1].get_pixmap(dpi=PREVIEW_DPI, alpha=False)
            cache[key] = tk.PhotoImage(data=base64.b64encode(pix.tobytes("png")).decode())
            if len(cache) > 40:
                cache.pop(next(iter(cache)))
        return cache[key]

    def _select_now(self):
        self._sel_job = None
        try:
            sel = self.tree.selection()
        except tk.TclError:
            return
        if not sel:
            return
        self.cur = int(sel[0])
        it = self.items[self.cur]
        self.scale = PREVIEW_DPI / 72
        self.img = self._preview(it["path"], it["page"])
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.img, anchor="nw")
        self.rect = None
        self._loading = True
        self.text.delete("1.0", "end")
        self.text.insert("1.0", self.texts[self.cur], "rtl")
        self.text.edit_modified(False)
        self._loading = False
        clip = self.clips[self.cur]
        if clip:
            self.rect = self.canvas.create_rectangle(*(v * self.scale for v in clip), outline=self._clip_color(),
                                                     width=px(2))

    _loading = False

    def _text_changed(self, _e=None):
        if self._loading or not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if self.cur >= 0:
            self.texts[self.cur] = self.text.get("1.0", "end-1c")

    def _read_now(self):
        if self.cur < 0 or not self.reader:
            return
        i, it = self.cur, self.items[self.cur]
        self.read_btn.configure(state="disabled", text=R("جارٍ القراءة… (بضع ثوانٍ)"))

        import queue
        import threading
        box: queue.Queue = queue.Queue()

        def work():
            try:
                box.put(("ok", self.reader(it, self._engine())))
            except Exception as e:
                box.put(("err", str(e) or type(e).__name__))

        def poll():  # الخيط الرئيسي وحده يلمس الواجهة
            try:
                if not self.win.winfo_exists():
                    return
                try:
                    kind, text = box.get_nowait()
                except queue.Empty:
                    self.win.after(100, poll)
                    return
                self._read_done(i, kind, text)
            except tk.TclError:
                pass

        threading.Thread(target=work, daemon=True).start()
        self.win.after(100, poll)

    def _read_done(self, i: int, kind: str, text: str):
        self.read_btn.configure(state="normal", text=R("اقرأها الآن وأرِني النص"))
        if kind == "err":
            self.status.configure(text=R("تعذّرت القراءة: " + text))
            return
        self.texts[i] = text
        self.status.configure(text=R("قرأ النموذج الصفحة — راجع النص ثم «اعتمد النص»."))
        if self.cur == i:
            self._loading = True
            self.text.delete("1.0", "end")
            self.text.insert("1.0", text, "rtl")
            self.text.edit_modified(False)
            self._loading = False

    def _accept(self):
        if self.cur < 0:
            return
        if not self.texts[self.cur].strip():
            self.text.focus_set()  # لا نعتمد نصاً فارغاً: اكتب ما تراه في الصورة أولاً
            return
        i = self.cur
        self.actions[i].set("accept")
        self._refresh(i)
        self._hide_row(i)
        # تأكيد واضح: الصف يتحوّل لـ «✓ اعتُمد» ونقفز للصفحة التالية؛ الحفظ الفعلي عند «نفّذ القرارات»
        nxt = next((k for k in range(i + 1, len(self.items)) if self.actions[k].get() == "later"), None)
        if nxt is not None:
            self.tree.selection_set(str(nxt))
            self.tree.focus(str(nxt))
            self.tree.see(str(nxt))
        self.status.configure(text=R(f"✓ اعتُمد نص الصفحة {self.items[i]['page']} (يُحفظ عند الضغط على «نفّذ القرارات»)"))

    def _drag_start(self, e):
        self._x0, self._y0 = e.x, e.y
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline=self._clip_color(), width=px(2))

    @staticmethod
    def _clip_color() -> str:
        return theme.palette()["err"]

    def _drag(self, e):
        if self.rect:
            self.canvas.coords(self.rect, self._x0, self._y0, e.x, e.y)

    def _drag_end(self, e):
        if self.cur < 0 or not self.rect:
            return
        x0, x1 = sorted((self._x0, e.x))
        y0, y1 = sorted((self._y0, e.y))
        if x1 - x0 < 10 or y1 - y0 < 10:
            self._clear_clip()
            return
        self.clips[self.cur] = tuple(round(v / self.scale, 1) for v in (x0, y0, x1, y1))
        self.actions[self.cur].set("send")
        self._refresh(self.cur)

    def _clear_clip(self):
        if self.cur >= 0:
            self.clips[self.cur] = None
            if self.rect:
                self.canvas.delete(self.rect)
                self.rect = None
            self._refresh(self.cur)

    def _set(self, action):
        sel = self.tree.selection()
        for s in sel:
            self.actions[int(s)].set(action)
            self._refresh(int(s))
        if sel and action == "send":
            self.status.configure(text=R(f"سُجّل القرار: تُرسل {len(sel)} صفحة لمزوّد الذكاء الاصطناعي "
                                         "عند إغلاق النافذة."))
        elif sel and action == "leave":
            for s in sel:
                self._hide_row(int(s))
            self.status.configure(text=R("تُركت الصفحة بلا نص (غير مقروء) — يُحفظ هذا عند إغلاق النافذة."))

    def _set_all(self, action):
        for i in range(len(self.items)):
            if self.actions[i].get() == "accept":   # ما اعتُمد لا يُلغى بقرار عام
                continue
            self.actions[i].set(action)
            self._refresh(i)

    def _engine(self) -> str:
        """مفتاح طريقة القراءة المختارة في القائمة المنسدلة."""
        try:
            label = self.engine_var.get()
        except AttributeError:
            return "auto"
        return next((k for k, lbl in self.engines if R(lbl) == label), "auto")

    def _hide_row(self, i: int) -> None:
        """الصفحة المحسومة (اعتُمدت أو تُركت) تختفي من القائمة؛ قرارها محفوظ في الذاكرة حتى الإغلاق."""
        nxt = next((k for k in range(i + 1, len(self.items)) if self.actions[k].get() == "later"), None)
        try:
            if nxt is not None:
                self.tree.selection_set(str(nxt))
                self.tree.focus(str(nxt))
                self.tree.see(str(nxt))
            self.tree.detach(str(i))
        except tk.TclError:
            pass

    def _toggle_read_all(self):
        if self._all_stop is not None:       # زر الإيقاف أثناء القراءة
            self._all_stop.set()
            self.all_btn.configure(text=R("جارٍ الإيقاف…"), state="disabled")
            return
        todo = [i for i in range(len(self.items)) if self.actions[i].get() in ("later", "send")]
        if not todo:
            return
        import queue
        import threading
        engine = self._engine()
        self._all_stop = stop = threading.Event()
        box: queue.Queue = queue.Queue()
        self.all_btn.configure(text=R("إيقاف القراءة"))

        def work():
            for i in todo:
                if stop.is_set():
                    break
                try:
                    box.put((i, "ok", self.reader(self.items[i], engine)))
                except Exception as e:
                    box.put((i, "err", str(e) or type(e).__name__))
            box.put((None, "end", ""))

        done = {"ok": 0, "err": 0}

        def poll():
            try:
                if not self.win.winfo_exists():
                    stop.set()
                    return
                while True:
                    try:
                        i, kind, text = box.get_nowait()
                    except queue.Empty:
                        break
                    if kind == "end":
                        self._all_stop = None
                        self.all_btn.configure(text=R("اقرأ الكل تلقائياً واحفظ"), state="normal")
                        self.status.configure(text=R(
                            f"انتهت القراءة: نجحت {done['ok']} صفحة" + (f"، وتعذّرت {done['err']} (بقيت في القائمة)" if done["err"] else "")
                            + ". اضغط «إغلاق» للحفظ."))
                        if not done["err"]:
                            self._close()
                        return
                    if kind == "ok":
                        self.texts[i] = text
                        self.actions[i].set("accept")
                        self._refresh(i)
                        self.tree.detach(str(i))
                        done["ok"] += 1
                    else:
                        done["err"] += 1
                        self.last_error = text
                    self.status.configure(text=R(f"قراءة تلقائية: تمت {done['ok']} من {len(todo)}"
                                                 + (f" — فشلت {done['err']}: {self.last_error[:80]}" if done["err"] else "")))
                self.win.after(200, poll)
            except tk.TclError:
                stop.set()

        self.last_error = ""
        threading.Thread(target=work, daemon=True).start()
        self.win.after(200, poll)

    def _close(self):
        """الإغلاق يحفظ كل ما اعتُمد أو تُرك؛ والصفحات التي بلا قرار تبقى معلّقة للمرة القادمة."""
        if self._all_stop is not None:
            self._all_stop.set()
        for d in self.__dict__.get("_docs", {}).values():
            d.close()
        self._apply()

    def _apply(self):
        decisions = [
            {**it, "action": self.actions[i].get(), "clip": self.clips[i], "text": self.texts[i]}
            for i, it in enumerate(self.items) if self.actions[i].get() != "later"
        ]
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        if decisions:
            self.on_apply(decisions)
