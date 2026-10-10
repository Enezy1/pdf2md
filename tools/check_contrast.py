"""يحسب تباين WCAG فعلياً لكل ثيم ووضع (AA: نص 4.5، عناصر الواجهة 3).

    python tools/check_contrast.py        # يخرج برمز 1 إن فشل أي زوج
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.ui.themes import THEMES  # noqa: E402


def lum(h: str) -> float:
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a: str, b: str) -> float:
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# (النص/العنصر، الخلفية، الحد الأدنى)
TEXT = 4.5
PAIRS = [
    ("fg", "bg", TEXT), ("fg", "card", TEXT), ("fg", "raised", TEXT), ("fg", "select", TEXT),
    ("muted", "bg", TEXT), ("muted", "card", TEXT), ("muted", "raised", TEXT),
    ("on_accent", "accent", TEXT),
    ("accent_text", "bg", TEXT), ("accent_text", "card", TEXT),
    ("ok", "bg", TEXT), ("ok", "card", TEXT),
    ("warn", "bg", TEXT), ("warn", "card", TEXT),
    ("err", "bg", TEXT), ("err", "card", TEXT),
    ("accent", "bg", 3.0), ("accent", "card", 3.0),   # عنصر غير نصي: مربع محدد، شريط تقدّم
    ("strong", "bg", 3.0), ("strong", "card", 3.0),   # حدود الحقول
]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    bad = 0
    for name, th in THEMES.items():
        if name == "classic":
            continue
        for mode in ("light", "dark"):
            t = th[mode]
            worst = 99.0
            for a, b, need in PAIRS:
                r = ratio(t[a], t[b])
                worst = min(worst, r)
                if r < need:
                    bad += 1
                    print(f"FAIL {name}/{mode}: {a} {t[a]} على {b} {t[b]} = {r:.2f} (المطلوب {need})")
            print(f"{name:7s} {mode:5s} أضعف زوج: {worst:.2f}")
    print("OK: كل الأزواج ≥ AA" if not bad else f"{bad} أزواج دون AA")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
