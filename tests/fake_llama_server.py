"""خادم وهمي يحاكي llama-server للاختبارات: python fake_llama_server.py <port> [reply|crash]"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])
MODE = sys.argv[2] if len(sys.argv) > 2 else "نص من الخادم الوهمي"
if MODE == "crash":
    sys.exit(7)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
        out = json.dumps({"choices": [{"message": {"content": "```markdown\n" + MODE + "\n```"},
                                       "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


HTTPServer(("127.0.0.1", PORT), H).serve_forever()
