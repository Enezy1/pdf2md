"""مصدر الألوان الوحيد للواجهة (tokens). أي لون يظهر في البرنامج يأتي من هنا ولا يُكتب في مكان آخر.

كل ثيم له وضعان (light / dark) وكل وضع له نفس الرموز:

  bg, deep      خلفية النافذة وطبقتها الأعمق (طرفا التدرّج الناعم)
  card, raised  البطاقات ثم العناصر المرفوعة فوقها (حقول، أزرار)
  line, strong  حدّ خفيف وحدّ واضح (حواف الحقول والمربعات)
  fg, muted     النص الأساسي والثانوي
  accent        تعبئة الزر الرئيسي "ابدأ" ومربع الاختيار وشريط التقدّم والمؤشر
  on_accent     نص فوق accent
  accent_text   لون تمييز للنصوص (روابط، معلومات) فوق bg و card
  accent2       لمسة التمييز الثانية (خط التدرّج الرفيع، التركيز)
  select        خلفية الصف المحدّد
  ok, warn, err "تم"، "تحتاج مراجعة"، الخطأ
  disabled      نص معطّل

الثيم "classic" هو المظهر الأصلي (sv-ttk الأزرق) ويبقى خياراً متاحاً؛ الافتراضي الآن "studio".
"""

from __future__ import annotations

import os

# ---- مظهر ويندوز 11 الأصلي: الافتراضي حتى يُختار ثيم آخر
_CLASSIC_LIGHT = {
    "bg": "#fafafa", "deep": "#fafafa", "card": "#ffffff", "raised": "#ffffff", "line": "#c4c4c4",
    "strong": "#8a8a8a", "fg": "#1a1a1a", "muted": "#5f5f5f", "accent": "#005fb8", "on_accent": "#ffffff",
    "accent_text": "#005fb8", "accent2": "#005fb8", "select": "#e7e7e7", "ok": "#0f7b0f", "warn": "#9d5d00",
    "err": "#c42b1c", "disabled": "#8a8a8a",
}
_CLASSIC_DARK = {
    "bg": "#1c1c1c", "deep": "#1c1c1c", "card": "#2b2b2b", "raised": "#2f2f2f", "line": "#404040",
    "strong": "#767676", "fg": "#ffffff", "muted": "#a0a0a0", "accent": "#57c8ff", "on_accent": "#000000",
    "accent_text": "#57c8ff", "accent2": "#57c8ff", "select": "#292929", "ok": "#6ccb5f", "warn": "#fce100",
    "err": "#ff99a4", "disabled": "#7a7a7a",
}

THEMES: dict[str, dict] = {
    "classic": {
        "label": "الأصلي (أزرق ويندوز)",
        "light": _CLASSIC_LIGHT, "dark": _CLASSIC_DARK,
    },
    # أخضر مزرق هادئ + لمسة ذهبية
    "teal": {
        "label": "أخضر مزرق مع ذهبي",
        "light": {
            "bg": "#e9f2f0", "deep": "#d9e9e6", "card": "#f6fbfa", "raised": "#ffffff", "line": "#b3cdc8",
            "strong": "#678a84", "fg": "#10302c", "muted": "#456561", "accent": "#0f766e", "on_accent": "#ffffff",
            "accent_text": "#0b645d", "accent2": "#c9921f", "select": "#c8e6e0", "ok": "#17703a",
            "warn": "#8f5400", "err": "#b42318", "disabled": "#7d9a95",
        },
        "dark": {
            "bg": "#0f1d1c", "deep": "#0a1514", "card": "#15292a", "raised": "#1c3637", "line": "#2d4d4b",
            "strong": "#648a86", "fg": "#e7f5f2", "muted": "#9dbcb7", "accent": "#2fc9b4", "on_accent": "#04211d",
            "accent_text": "#5fe3d1", "accent2": "#e8b84a", "select": "#1f4a47", "ok": "#6fd48e",
            "warn": "#f4b942", "err": "#ff9088", "disabled": "#5f7f7a",
        },
    },
    # أزرق عميق + برتقالي دافئ
    "blue": {
        "label": "أزرق عميق مع برتقالي",
        "light": {
            "bg": "#e8eef8", "deep": "#d8e2f2", "card": "#f6f9fd", "raised": "#ffffff", "line": "#b3c3dc",
            "strong": "#6f84a8", "fg": "#122242", "muted": "#475979", "accent": "#1d4fb8", "on_accent": "#ffffff",
            "accent_text": "#1b4aa8", "accent2": "#e8782e", "select": "#cddcf4", "ok": "#17703a",
            "warn": "#8f4a00", "err": "#b42318", "disabled": "#7f92b0",
        },
        "dark": {
            "bg": "#0d1626", "deep": "#080f1b", "card": "#142139", "raised": "#1b2d4d", "line": "#2c4067",
            "strong": "#677fab", "fg": "#e8eefb", "muted": "#a0b2d2", "accent": "#5b94ff", "on_accent": "#05122b",
            "accent_text": "#86b3ff", "accent2": "#ff9d52", "select": "#203d73", "ok": "#6fd48e",
            "warn": "#f6b94a", "err": "#ff9088", "disabled": "#62789f",
        },
    },
    # كحلي عميق بروح DocConvert Studio: أزرق مع نهاية بنفسجية لزر "ابدأ"
    "studio": {
        "label": "Studio (أزرق ويندوز هادئ)",
        "light": {
            "bg": "#f3f5f9", "deep": "#e8ecf2", "card": "#ffffff", "raised": "#ffffff", "line": "#dde2eb",
            "strong": "#74839d", "fg": "#1b2230", "muted": "#566176", "accent": "#0f6cbd", "on_accent": "#ffffff",
            "accent_text": "#0a5aa3", "accent2": "#0f6cbd", "select": "#dcebf8", "ok": "#0e7a52",
            "warn": "#8f5a00", "err": "#b42318", "disabled": "#8c98ad",
        },
        "dark": {
            "bg": "#0e1218", "deep": "#090c11", "card": "#151a22", "raised": "#1d2430", "line": "#28303d",
            "strong": "#5f6f88", "fg": "#e8edf5", "muted": "#9aa5b8", "accent": "#5ab4ee", "on_accent": "#06243a",
            "accent_text": "#86c9f6", "accent2": "#5ab4ee", "select": "#17324a", "ok": "#4fcf9e",
            "warn": "#f0b84a", "err": "#f28b82", "disabled": "#5f6f88",
        },
    },
    # بنفسجي/نيلي ناعم + مرجاني وردي
    "indigo": {
        "label": "نيلي ناعم مع مرجاني",
        "light": {
            "bg": "#eceaf8", "deep": "#dedbf2", "card": "#f9f8fe", "raised": "#ffffff", "line": "#bfbbe0",
            "strong": "#8279ae", "fg": "#1d1840", "muted": "#524d78", "accent": "#5a44c8", "on_accent": "#ffffff",
            "accent_text": "#4b36b0", "accent2": "#ee6a5f", "select": "#d9d4f5", "ok": "#17703a",
            "warn": "#8f4a00", "err": "#b42318", "disabled": "#8c88b0",
        },
        "dark": {
            "bg": "#141229", "deep": "#0d0b1e", "card": "#1c1a39", "raised": "#26234c", "line": "#38356a",
            "strong": "#7a75b5", "fg": "#ece9fb", "muted": "#aca8d6", "accent": "#8d7dff", "on_accent": "#100b33",
            "accent_text": "#b3a8ff", "accent2": "#ff8c7c", "select": "#38327e", "ok": "#6fd48e",
            "warn": "#f6b94a", "err": "#ff9088", "disabled": "#6c68a0",
        },
    },
}

DEFAULT = "studio"  # الثيم المعتمد (على طلب المستخدم: تصميم Studio الداكن)


def resolve(name: str = "") -> str:
    """اسم الثيم الفعلي: متغير البيئة PDF2MD_PALETTE (تبديل مؤقت للتجربة) ثم الإعداد ثم الأصلي."""
    for cand in (os.environ.get("PDF2MD_PALETTE", ""), name):
        if cand in THEMES:
            return cand
    return DEFAULT


def tokens(name: str, dark: bool) -> dict[str, str]:
    return THEMES[resolve(name)]["dark" if dark else "light"]


def labels() -> dict[str, str]:
    return {k: v["label"] for k, v in THEMES.items()}
