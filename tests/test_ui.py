"""اختبارات الواجهة: الوضع يتحكم في القفل، و"عبر المواقع" معطّل بلا مفتاح، والتشخيص بلا مفاتيح."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PDF2MD_DATA_DIR", tempfile.mkdtemp(prefix="pdf2md-test-data-"))

import tkinter as tk  # noqa: E402

from app.cloud_gate import CLOUD, LOCAL  # noqa: E402


class FakeSecrets:
    def __init__(self, keys=None):
        self.keys = dict(keys or {})

    def has_key(self, pid):
        return pid in self.keys

    def get_key(self, pid):
        return self.keys.get(pid)


class MainWindowModeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f"لا توجد شاشة: {e}")
        self.root.withdraw()
        from app.ui.main_window import MainWindow
        self.app = MainWindow(self.root)
        self.app.secrets = FakeSecrets()

    def tearDown(self):
        self.root.destroy()

    def test_cloud_disabled_without_key_and_gate_local(self):
        self.app._set_mode("api")
        self.app._refresh_mode()
        self.assertEqual(self.app.mode_var.get(), "local")
        self.assertEqual(str(self.app.mode_buttons["api"].cget("state")), "disabled")
        self.assertEqual(self.app.gate.mode, LOCAL)

    def test_gate_follows_mode_when_provider_active(self):
        cfg = self.app.provider_config
        pid = next(iter(cfg.specs))
        cfg.state(pid).enabled = True
        self.app.secrets = FakeSecrets({pid: "k"})
        self.app._refresh_mode()
        self.assertEqual(str(self.app.mode_buttons["api"].cget("state")), "normal")
        self.app._set_mode("api")
        self.assertEqual(self.app.gate.mode, CLOUD)
        self.app._set_mode("local")
        self.assertEqual(self.app.gate.mode, LOCAL)

    def test_cloud_notice_cancel_keeps_local(self):
        self.app.settings.cloud_notice_dismissed = False
        self.app._cloud_notice = lambda: (False, False)
        self.app.mode_var.set("api")
        self.app._on_mode()
        self.assertEqual(self.app.mode_var.get(), "local")
        self.assertEqual(self.app.gate.mode, LOCAL)


class DiagnosticsTests(unittest.TestCase):
    def test_export_has_no_keys(self):
        from app.config import app_data_dir
        from app.diagnostics import export
        from app.secrets_store import register_secret
        register_secret("sk-THIS-IS-SECRET-123456")
        log_dir = app_data_dir() / "logs"
        log_dir.mkdir(exist_ok=True)
        (log_dir / "app.log").write_text("key=sk-THIS-IS-SECRET-123456\n", encoding="utf-8")
        out = export(Path(tempfile.mkdtemp()) / "d.zip")
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            self.assertIn("info.json", names)
            self.assertNotIn("sk-THIS-IS-SECRET", z.read("logs/app.log").decode("utf-8"))


if __name__ == "__main__":
    unittest.main()


class AutoModeTests(MainWindowModeTests):
    """"تلقائي": متاح دائماً؛ البوابة تُفتح فقط مع مزوّد مفعّل، والصفحات المصوّرة تُرسل حسب الموجّه."""

    def test_auto_without_provider_stays_local(self):
        self.app._set_mode("auto")
        self.app._refresh_mode()
        self.assertEqual(self.app.mode_var.get(), "auto")
        self.assertEqual(str(self.app.mode_buttons["auto"].cget("state")), "normal")
        self.assertEqual(self.app.gate.mode, LOCAL)
        self.assertTrue(self.app.settings.api_for_scanned)

    def test_auto_with_provider_opens_gate(self):
        cfg = self.app.provider_config
        pid = next(iter(cfg.specs))
        cfg.state(pid).enabled = True
        self.app.secrets = FakeSecrets({pid: "k"})
        self.app._refresh_mode()
        self.app._set_mode("auto")
        self.assertEqual(self.app.gate.mode, CLOUD)
        self.assertTrue(self.app._auto_tiers())
