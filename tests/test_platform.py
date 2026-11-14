"""اختبارات التوافق مع أنظمة التشغيل: مسارات البيانات، وحفظ المفاتيح، ورموز الأيقونات.

هذي اختبارات لا تحتاج ماك: تتحقق من المنطق نفسه الذي سيُنفَّذ هناك،
وتحرس رموز أيقونات ويندوز من التغيير العَرَضي.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PDF2MD_DATA_DIR", tempfile.mkdtemp(prefix="pdf2md-test-data-"))

from app import oscompat  # noqa: E402
from app.config import app_data_dir  # noqa: E402
from app.secrets_store import SecretsStore, _LocalBackend  # noqa: E402
from app.ui import ctk_ui  # noqa: E402


class DataDirTests(unittest.TestCase):
    def test_app_data_dir_is_inside_the_system_data_folder(self):
        # بدون تجاوز المتغيّر: المسار الافتراضي هو مجلّد بيانات النظام.
        override = os.environ.pop("PDF2MD_DATA_DIR", None)
        try:
            d = app_data_dir()
        finally:
            if override is not None:
                os.environ["PDF2MD_DATA_DIR"] = override
        self.assertTrue(d.is_dir())
        self.assertEqual(d.name, "pdf2md")
        if sys.platform == "darwin":
            self.assertIn("Application Support", str(d))
        elif os.name == "nt":
            self.assertIn("pdf2md", str(d))
        else:
            self.assertIn("pdf2md", str(d))

    def test_override_wins(self):
        with tempfile.TemporaryDirectory() as t:
            os.environ["PDF2MD_DATA_DIR"] = t
            try:
                self.assertEqual(app_data_dir(), Path(t))
            finally:
                os.environ.pop("PDF2MD_DATA_DIR", None)


class OpenPathTests(unittest.TestCase):
    def test_missing_path_is_not_opened(self):
        self.assertFalse(oscompat.open_path(Path(tempfile.gettempdir()) / "pdf2md-does-not-exist-xyz"))

    def test_existing_file_is_handled_without_raising(self):
        f = Path(tempfile.mkdtemp()) / "a.txt"
        f.write_text("x", encoding="utf-8")
        # على ويندوز يفتح os.startfile الملف؛ على ماك يفتح "open". المهم: بلا استثناء.
        oscompat.open_path(f)


class MemoryTests(unittest.TestCase):
    def test_memory_is_a_positive_number_or_none(self):
        m = oscompat.memory_gb()
        self.assertTrue(m is None or (isinstance(m, float) and m > 0))


class GpuTests(unittest.TestCase):
    def test_rows_are_well_formed(self):
        for row in oscompat.gpu_rows():
            self.assertIn("name", row)
            self.assertIsInstance(row["vram_gb"], float)

    def test_macos_parse_ignores_empty_names(self):
        from app import gpu_info
        self.assertEqual(gpu_info.parse_macos([{"name": "", "vram_gb": 4}]), [])


class LocalSecretsTests(unittest.TestCase):
    """الاحتياطي المحلي للمفاتيح: يعمل على غير ويندوز حين لا تتوفّر ذاكرة مفاتيح النظام."""

    def test_roundtrip_and_not_plaintext(self):
        path = Path(tempfile.mkdtemp()) / "secrets.dat"
        store = SecretsStore(_LocalBackend(path))
        store.set_key("groq", "sk-LOCAL-SECRET-0123456789")
        self.assertEqual(store.get_key("groq"), "sk-LOCAL-SECRET-0123456789")
        raw = path.read_bytes()
        self.assertNotIn(b"sk-LOCAL-SECRET-0123456789", raw)

    def test_delete_removes_the_key(self):
        path = Path(tempfile.mkdtemp()) / "secrets.dat"
        store = SecretsStore(_LocalBackend(path))
        store.set_key("groq", "sk-LOCAL-SECRET-0123456789")
        store.delete_key("groq")
        self.assertIsNone(store.get_key("groq"))

    def test_wrong_key_cannot_decrypt(self):
        path = Path(tempfile.mkdtemp()) / "secrets.dat"
        store = SecretsStore(_LocalBackend(path))
        store.set_key("groq", "sk-LOCAL-SECRET-0123456789")
        other = _LocalBackend(path)
        other._key = b"x" * 32
        with self.assertRaises(OSError):
            other.get("api_key:groq")


class IconTests(unittest.TestCase):
    """رموز الأيقونات: لكل محرف واحد بالضبط، ورموز ويندوز لم تتغيّر."""

    EXPECTED_WIN = {
        "settings": 0xE713, "folder": 0xE8B7, "add": 0xE710, "trash": 0xE74D,
        "history": 0xE81C, "doc": 0xE8A5, "more": 0xE712, "check": 0xE73E,
        "cloud": 0xE753, "shield": 0xEA18, "upload": 0xE898, "arrow": 0xE72B,
        "layers": 0xE81E, "chip": 0xE950,
    }

    def test_same_icon_set_on_both_platforms(self):
        self.assertEqual(set(ctk_ui._ICON_WIN), set(ctk_ui._ICON_MAC))
        self.assertEqual(set(ctk_ui._ICON_WIN), set(ctk_ui.ICON))

    def test_windows_codepoints_are_unchanged(self):
        for key, cp in self.EXPECTED_WIN.items():
            self.assertEqual(ord(ctk_ui._ICON_WIN[key]), cp, key)

    def test_every_glyph_is_one_character(self):
        for source in (ctk_ui._ICON_WIN, ctk_ui.ICON):
            for key, glyph in source.items():
                self.assertEqual(len(glyph), 1, f"{key}: {glyph!r}")

    def test_mac_glyphs_are_not_private_use(self):
        """نطاق الاستخدام الخاص (E000–F8FF) لا يرسمه Tk إلا بخط Fluent — وهو لويندوز فقط."""
        for key, (first, second) in ctk_ui._ICON_MAC.items():
            for glyph in (first, second):
                self.assertFalse(0xE000 <= ord(glyph) <= 0xF8FF, f"{key}: {glyph!r}")

    def test_icon_family_falls_back_gracefully(self):
        fam = ctk_ui._first_family(("خط غير موجود أبداً",), "Segoe UI Symbol")
        self.assertEqual(fam, "Segoe UI Symbol")


class FontTests(unittest.TestCase):
    def test_font_candidates_cover_the_platform(self):
        from app.ui import theme
        self.assertIn(sys.platform, theme._FONT_CANDIDATES)
        self.assertIn("win32", theme._FONT_CANDIDATES)
        self.assertIn("darwin", theme._FONT_CANDIDATES)

    def test_apple_symbols_is_a_mac_icon_candidate(self):
        self.assertIn("Apple Symbols", ctk_ui._ICON_FONTS_MAC)
        self.assertIn("Segoe Fluent Icons", ctk_ui._ICON_FONTS_WIN)


class KeyCodeTests(unittest.TestCase):
    """رموز الأزرار تختلف بين ويندوز وماك — والاختصارات يجب أن تعمل على الاثنين."""

    def test_all_platforms_define_the_keys_we_use(self):
        from app.ui import theme
        for plat, table in theme.KEYCODES.items():
            for key in ("a", "c", "o", "v", "x", "comma"):
                self.assertIn(key, table, plat)
                self.assertIsInstance(table[key], int)

    def test_windows_and_mac_use_different_codes_for_o(self):
        from app.ui import theme
        self.assertNotEqual(theme.KEYCODES["win32"]["o"], theme.KEYCODES["darwin"]["o"])
        self.assertEqual(theme.KEYCODES["win32"]["o"], 79)
        self.assertEqual(theme.KEYCODES["darwin"]["o"], 31)

    def test_key_code_follows_the_running_platform(self):
        from app.ui import theme
        self.assertEqual(theme.key_code("o"), theme.KEYCODES[sys.platform]["o"])

    def test_clipboard_map_matches_the_platform(self):
        from app.ui import clipboard, theme
        self.assertIn(theme.key_code("v"), clipboard.VK)
        self.assertEqual(clipboard.VK[theme.key_code("v")], "<<Paste>>")


if __name__ == "__main__":
    unittest.main()
