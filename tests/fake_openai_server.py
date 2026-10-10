"""خادم وهمي بصيغة OpenAI chat/completions للاختبارات (127.0.0.1 فقط، بدون أي طلب حقيقي).

السلوك يُضبط عبر FakeOpenAI.behavior: دالة (page_numbers, call_index) → (status, headers, text).
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE_RE = re.compile(r"\[image for page (\d+)\]")


def good_text(pages: list[int]) -> str:
    return "\n".join(f"<!-- page:{p} -->\n# صفحة {p}\n" + " ".join(f"السطر {k} من الصفحة {p}: نص تجريبي رقم {k * 7 + p}." for k in range(40)) for p in pages)


class FakeOpenAI:
    def __init__(self, behavior=None):
        self.behavior = behavior or (lambda pages, i: (200, {}, good_text(pages)))
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                texts = [c["text"] for c in body["messages"][0]["content"] if c["type"] == "text"]
                pages = [int(m) for t in texts for m in PAGE_RE.findall(t)]
                images = sum(1 for c in body["messages"][0]["content"] if c["type"] == "image_url")
                with outer.lock:
                    idx = len(outer.calls)
                    outer.calls.append({"pages": pages, "images": images, "auth": self.headers.get("Authorization"),
                                        "path": self.path, "model": body.get("model")})
                status, headers, text = outer.behavior(pages, idx)
                payload = json.dumps({"choices": [{"message": {"content": text}}]} if status == 200
                                     else {"error": {"message": text}}).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
