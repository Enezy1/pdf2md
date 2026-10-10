"""worker وهمي لـ PaddleOCR بنفس بروتوكول app/paddle_worker.py (للاختبارات فقط).

FAKE_PADDLE_MODE: ok | fail (رد ok=false) | crash (يخرج عند أول طلب) | noready
FAKE_PADDLE_LINES: JSON قائمة الأسطر التي تُعاد لكل صفحة ({"text","conf","box"})
يطبّق حظر الشبكة الحقيقي من app/paddle_worker.py ويبلّغ نتيجته في رسالة الجاهزية.
"""

import importlib.util
import json
import os
import socket
import sys

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("pw", os.path.join(here, "..", "app", "paddle_worker.py"))
pw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pw)
pw.block_network()
sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")

blocked = False
try:
    socket.create_connection(("127.0.0.1", 9), timeout=1)
except pw.NetworkBlocked:
    blocked = True
except OSError:
    pass

mode = os.environ.get("FAKE_PADDLE_MODE", "ok")
lines = json.loads(os.environ.get("FAKE_PADDLE_LINES", "[]"))
log = os.environ.get("FAKE_PADDLE_LOG")


def send(o):
    sys.stdout.write(json.dumps(o, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if mode == "noready":
    send({"ready": False, "error": "fake failure"})
    sys.exit(1)
send({"ready": True, "net_blocked": blocked, "argv": sys.argv[1:], "nosite": sys.flags.no_site, "isolated": sys.flags.isolated})
for raw in sys.stdin:
    req = json.loads(raw)
    if req.get("op") == "quit":
        break
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("ocr\n")
    if mode == "crash":
        sys.exit(3)
    if mode == "fail":
        send({"id": req["id"], "ok": False, "error": "fake error"})
        continue
    send({"id": req["id"], "ok": True, "lines": lines, "ms": 5})
