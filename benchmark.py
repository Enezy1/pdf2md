"""مقارنة النماذج المحلية من سطر الأوامر — المنطق كله في app/model_lab.py
(نفسه الذي يشغّله "مختبر النماذج" في تبويب "داخلي").

    python benchmark.py "C:\\path\\file.pdf" --pages 2,5,9
    python benchmark.py --ocr-chain "C:\\path\\file.pdf" --pages 2,5,9
        مقارنة OCR.space خام / OCR.space + المصحّح / نموذج الرؤية (app/ocr_bench.py)
"""

from __future__ import annotations

import sys

from app.model_lab import *  # noqa: F401,F403
from app.model_lab import main

if __name__ == "__main__":
    if "--ocr-chain" in sys.argv:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        from app.ocr_bench import main as ocr_main
        sys.exit(ocr_main([a for a in sys.argv[1:] if a != "--ocr-chain"]))
    sys.exit(main())
