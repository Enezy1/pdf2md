"""لصق/نسخ/قص في حقول الإدخال مهما كانت لغة لوحة المفاتيح.

مشكلة Tk على ويندوز: مع لوحة المفاتيح العربية يصبح Ctrl+V حرفاً عربياً (ر) فلا يعمل اللصق.
الحل: الربط برمز الزر الفعلي (keycode) لا بالحرف، مع قائمة بالزر الأيمن.

رموز الأزرار تختلف بين ويندوز وماك، فنأخذها من theme.key_code (انظر theme.KEYCODES).
"""

from __future__ import annotations

import sys

import tkinter as tk

from .theme import R, key_code

# رموز الأزرار الافتراضية — لا تتغير مع لغة لوحة المفاتيح
VK = {key_code("v"): "<<Paste>>", key_code("c"): "<<Copy>>",
      key_code("x"): "<<Cut>>", key_code("a"): "<<SelectAll>>"}
_LATIN = {"v", "c", "x", "a", "V", "C", "X", "A"}


def _on_ctrl_key(event: tk.Event):
    # الحروف اللاتينية يعالجها Tk بنفسه؛ نتدخل فقط عندما تكون لغة اللوحة غير لاتينية
    if event.keysym in _LATIN:
        return None
    action = VK.get(event.keycode)
    if not action:
        return None
    w = event.widget
    if action == "<<SelectAll>>":
        try:
            w.select_range(0, "end")
            w.icursor("end")
        except tk.TclError:
            w.tag_add("sel", "1.0", "end")
    else:
        w.event_generate(action)
    return "break"


def _context_menu(event: tk.Event):
    w = event.widget
    m = tk.Menu(w, tearoff=0)
    m.add_command(label=R("لصق"), command=lambda: w.event_generate("<<Paste>>"))
    m.add_command(label=R("نسخ"), command=lambda: w.event_generate("<<Copy>>"))
    m.add_command(label=R("قص"), command=lambda: w.event_generate("<<Cut>>"))
    m.add_separator()
    m.add_command(label=R("مسح الحقل"), command=lambda: _clear(w))
    w.focus_set()
    m.tk_popup(event.x_root, event.y_root)


def _clear(w):
    try:
        w.delete(0, "end")
    except tk.TclError:
        w.delete("1.0", "end")


def paste_into(entry: tk.Widget) -> None:
    """زر "لصق": يضع محتوى الحافظة في الحقل بدل محتواه."""
    try:
        text = entry.clipboard_get()
    except tk.TclError:
        return
    entry.delete(0, "end")
    entry.insert(0, text.strip())


def install(root: tk.Misc) -> None:
    for cls in ("TEntry", "Entry", "TCombobox", "TSpinbox", "Text"):
        root.bind_class(cls, "<Control-KeyPress>", _on_ctrl_key, add="+")
        root.bind_class(cls, "<Button-3>", _context_menu, add="+")
        if sys.platform == "darwin":
            # على ماك: ⌘V هو اللصق الطبيعي (و Ctrl+V ما زال مدعوماً أيضاً).
            root.bind_class(cls, "<Command-KeyPress>", _on_ctrl_key, add="+")
