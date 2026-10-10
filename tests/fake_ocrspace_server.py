"""خادم وهمي بصيغة OCR.space (127.0.0.1 فقط، بدون أي طلب حقيقي).

behavior(call_index, form) → (status, payload_dict).
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def ok_payload(text: str) -> dict:
    return {"ParsedResults": [{"ParsedText": text, "FileParseExitCode": 1, "ErrorMessage": ""}],
            "OCRExitCode": 1, "IsErroredOnProcessing": False, "ProcessingTimeInMilliseconds": "120"}


def error_payload(message: str) -> dict:
    return {"ParsedResults": [], "OCRExitCode": 3, "IsErroredOnProcessing": True, "ErrorMessage": [message]}


class FakeOcrSpace:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"])).decode()
                form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
                with outer.lock:
                    idx = len(outer.calls)
                    outer.calls.append({"apikey": self.headers.get("apikey"), "form": form,
                                        "image_bytes": len(form.get("base64Image", "")) * 3 // 4})
                status, payload = outer.behavior(idx, form)
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/parse/image"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
