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
    {..item, "action": "send"|"leave"|"later", "clip": (x0,y0,x1,y1) بنقاط PDF أو None}."""

    def __init__(self, parent: tk.Misc, items: list[dict], on_apply):
        self.items = items
        self.on_apply = on_apply
        self.actions = [tk.StringVar(value="later") for _ in items]
        self.clips: list[tuple | None] = [None] * len(items)
        self.cur = -1
        self.win = tk.Toplevel(parent)
        self.win.title(R("مراجعة الصفحات الضعيفة"))
        theme.fit_window(self.win, px(980), px(720), px(640), px(480))
        theme.dark_titlebar(self.win)

        theme.Para(self.win, text=(
            "هذه صفحات لم تُقرأ جيداً. اختر لكل صفحة: أرسلها لمزوّد، أو اتركها [غير مقروء]. "
            "لإرسال جزء فقط: اسحب بالفأرة على الصورة لتحديد قصاصة. "
            "بلوحة المفاتيح: الأسهم للتنقل، Enter أرسل، Delete اتركها.")).pack(fill="x", padx=px(S2), pady=px(S2))

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
        self.tree.bind("<Return>", lambda e: self._set("send"))
        self.tree.bind("<Delete>", lambda e: self._set("leave"))
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self.tree.pack(side="right", fill="y")

        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(right, bg=theme.palette()["card"], highlightthickness=0, width=px(520), height=px(600))
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.rect = None
        self.scale = 1.0

        bar = ttk.Frame(self.win)
        bar.pack(fill="x", padx=px(S2), pady=px(S2))
        ttk.Button(bar, text=R("أرسل لمزوّد"), command=lambda: self._set("send")).pack(side="right")
        ttk.Button(bar, text=R("اتركها [غير مقروء]"), command=lambda: self._set("leave")).pack(side="right", padx=px(S1))
        ttk.Button(bar, text=R("إلغاء القصاصة"), command=self._clear_clip).pack(side="right", padx=px(S1))
        ttk.Button(bar, text=R("الكل: أرسل"), command=lambda: self._set_all("send")).pack(side="right", padx=(0, px(S3)))
        ttk.Button(bar, text=R("الكل: اتركها"), command=lambda: self._set_all("leave")).pack(side="right", padx=px(S1))
        ttk.Button(bar, text=R("تطبيق"), style="Accent.TButton", command=self._apply).pack(side="left")
        ttk.Button(bar, text=R("لاحقاً"), command=self.win.destroy).pack(side="left", padx=px(S1))
        if items:
            self.tree.selection_set("0")
            self.tree.focus("0")
            self.tree.focus_set()

    def _refresh(self, i: int):
        it = self.items[i]
        a = {"send": "أرسل", "leave": "اتركها", "later": "—"}[self.actions[i].get()]
        if self.clips[i] and self.actions[i].get() == "send":
            a += " (قصاصة)"
        self.tree.item(str(i), values=(R(a), R(WEAK_REASONS.get(it["reason"], it["reason"])), it["page"],
                                       R(Path(it["path"]).name)))

    def _select(self):
        sel = self.tree.selection()
        if not sel:
            return
        self.cur = int(sel[0])
        it = self.items[self.cur]
        with fitz.open(it["path"]) as doc:
            pix = doc[it["page"] - 1].get_pixmap(dpi=PREVIEW_DPI, alpha=False)
            data = pix.tobytes("png")
        self.scale = PREVIEW_DPI / 72
        self.img = tk.PhotoImage(data=base64.b64encode(data).decode())
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.img, anchor="nw")
        self.rect = None
        clip = self.clips[self.cur]
        if clip:
            self.rect = self.canvas.create_rectangle(*(v * self.scale for v in clip), outline=self._clip_color(),
                                                     width=px(2))

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
        for s in self.tree.selection():
            self.actions[int(s)].set(action)
            self._refresh(int(s))

    def _set_all(self, action):
        for i in range(len(self.items)):
            self.actions[i].set(action)
            self._refresh(i)

    def _apply(self):
        decisions = [
            {**it, "action": self.actions[i].get(), "clip": self.clips[i]}
            for i, it in enumerate(self.items) if self.actions[i].get() != "later"
        ]
        self.win.destroy()
        if decisions:
            self.on_apply(decisions)
