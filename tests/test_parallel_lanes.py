import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import pymupdf as fitz

from app.dispatcher import ApiRunner


class ParallelLanesTest(unittest.TestCase):
    def test_chunks_alternate_between_lanes_and_run_together(self):
        d = tempfile.mkdtemp()
        pdf = str(Path(d) / "a.pdf")
        doc = fitz.open()
        doc.new_page()
        doc.save(pdf)
        doc.close()

        r = ApiRunner.__new__(ApiRunner)
        r.report = SimpleNamespace(down={})
        r.emit = lambda *a, **k: None
        calls, active, peak = [], [0], [0]
        lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=5)

        def fake_send(doc_, chunk, state, job, lane):
            with lock:
                calls.append((tuple(chunk), lane.name))
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            if len(calls) <= 2:
                barrier.wait()      # يفشل بالمهلة إن لم يعمل المساران معاً
            with lock:
                active[0] -= 1

        r._send_chunk = fake_send
        mk = lambda n: SimpleNamespace(name=n, id="gemini", provider=SimpleNamespace(model=n),
                                       limiter=SimpleNamespace(usage=SimpleNamespace(get=lambda k: 0), pid="x"))
        a, b = mk("lite35"), mk("lite31")
        r._send_parallel([[1, 2], [3, 4], [5]], None, SimpleNamespace(path=pdf, name="a.pdf"), a, [a, b])
        self.assertEqual({c: n for c, n in calls}, {(1, 2): "lite35", (3, 4): "lite31", (5,): "lite35"})
        self.assertEqual(peak[0], 2)


if __name__ == "__main__":
    unittest.main()
