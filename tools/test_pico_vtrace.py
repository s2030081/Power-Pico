"""Tests for pico_vtrace.py on the synthetic bench stream.

bench_stream (test_pico_stream.py): 4.2 V, a step to 3.7 V at 1 s, a hard
reset at 3 s (0 V for 0.8 s, then 5 V for 0.5 s), then 3.7 V; device time
starts at 1 s, so each event is 1 s later in device time.

Run: py tools/test_pico_vtrace.py
"""

import io
import contextlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pico_vtrace as vt  # noqa: E402
from test_pico_stream import adc_packet, bench_stream  # noqa: E402


class Analysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.gaps, cls.stale = vt.read_rows(bench_stream(skip=range(5000, 5010)))

    def test_rows_and_gaps(self):
        self.assertEqual(len(self.rows), 5990)
        self.assertIsNone(self.stale)
        self.assertEqual(len(self.gaps), 1)
        before, after = self.gaps[0]
        self.assertAlmostEqual(before, 5.999)
        self.assertAlmostEqual(after - before, 0.011)

    def test_under_1_v_and_over_4_75_v(self):
        [low] = vt.spans(self.rows, lambda lo, hi: (lo + hi) / 2 < vt.LOW)
        self.assertAlmostEqual(low[0], 4.0, delta=0.002)
        self.assertAlmostEqual(low[1], 4.799, delta=0.002)
        [high] = vt.spans(self.rows, lambda lo, hi: (lo + hi) / 2 > vt.HIGH)
        self.assertAlmostEqual(high[0], 4.8, delta=0.002)
        self.assertAlmostEqual(high[1], 5.299, delta=0.002)
        self.assertAlmostEqual(high[3], 5.0, delta=0.01)

    def test_plateaus(self):
        # The 3.7 V plateau starts after the 50 ms ramp; the others within a
        # block of their step.
        found = vt.plateaus(self.rows)
        want = [(1.0, 4.2), (2.05, 3.7), (4.0, 0.0), (4.8, 5.0), (5.3, 3.7)]
        self.assertEqual(len(found), len(want))
        for p, (start, volts) in zip(found, want):
            self.assertAlmostEqual(p["start"], start, delta=0.021)
            self.assertAlmostEqual(p["mean"], volts, delta=0.005)

    def test_window(self):
        rows, gaps, _ = vt.read_rows(bench_stream(skip=range(5000, 5010)), t0=2.5, t1=3.0)
        self.assertEqual(len(rows), 501)
        self.assertEqual(len(gaps), 1)  # gaps are found over the whole file


class Files(unittest.TestCase):
    def test_a_stale_first_packet_is_dropped(self):
        data = adc_packet(1000, [4.0] * 10) + bench_stream(packets=200, start_us=5000000)
        rows, gaps, stale = vt.read_rows(data)
        self.assertAlmostEqual(stale, 0.001)
        self.assertEqual((len(rows), gaps), (200, []))

    def test_the_command_line(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "run-1.bin")
            picture = os.path.join(folder, "run.svg")
            with open(path, "wb") as handle:
                handle.write(bench_stream())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(vt.main([path, "--svg", picture]), 0)
            with open(picture) as handle:
                self.assertEqual(handle.read().count("<polyline"), 2)
        text = out.getvalue()
        self.assertIn("packets 6000, device time 1.000 .. 6.999 s", text)
        self.assertIn("stream gaps over 1.5 ms: 0", text)
        self.assertIn("  4.000 .. 4.799 (799 ms) min 0.000", text)

    def test_an_empty_file(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "empty.bin")
            open(path, "wb").close()
            with contextlib.redirect_stdout(out):
                self.assertEqual(vt.main([path]), 1)
        self.assertIn("no packets", out.getvalue())


if __name__ == "__main__":
    unittest.main()
