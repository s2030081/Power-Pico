"""Tests for pico_vlog.py and pico_dlog.py against a simulated port.

Run: py tools/test_pico_vlog.py
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pico_dlog  # noqa: E402
import pico_serial  # noqa: E402
import pico_stream  # noqa: E402
import pico_vlog  # noqa: E402
from test_pico_stream import adc_packet, bench_stream, debug_frame, event  # noqa: E402


class FakePort:
    """Serves `chunks` (None: a read that times out), then is gone."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def read(self, n):
        if not self.chunks:
            raise OSError("the device is gone")
        chunk = self.chunks.pop(0)
        return chunk or b""

    def close(self):
        self.closed = True


def pieces(data, size=4096):
    return [data[i:i + size] for i in range(0, len(data), size)]


class Logging(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.prefix = os.path.join(self.folder.name, "run")
        self.ports = []
        self.lines = []

        def open_port(name, timeout):
            if not self.ports:
                raise OSError("cannot open %s" % name)
            return self.ports.pop(0)

        for patch in (mock.patch.object(pico_serial, "list_ports",
                                        lambda: [pico_serial.Port("COM3", "USB Serial Device")]),
                      mock.patch.object(pico_serial, "open_port", open_port)):
            patch.start()
            self.addCleanup(patch.stop)

    def log(self, **kwargs):
        options = dict(wait=0.2, max_seconds=30, say=self.lines.append, poll=0.01, interval=0)
        options.update(kwargs)
        return pico_vlog.log(self.prefix, **options)

    def read(self, segment):
        with open("%s-%d.bin" % (self.prefix, segment), "rb") as handle:
            return handle.read()

    def test_every_byte_is_kept_across_a_lost_port(self):
        data = bench_stream(packets=300)
        half = len(data) // 2
        first, second = FakePort(pieces(data[:half])), FakePort(pieces(data[half:]) + [None])
        self.ports = [first, second]
        self.assertEqual(self.log(), 0)
        self.assertEqual(self.read(1) + self.read(2), data)
        self.assertTrue(first.closed and second.closed)
        text = "\n".join(self.lines)
        self.assertIn("opened COM3 -> %s-1.bin" % self.prefix, text)
        self.assertIn("opened COM3 -> %s-2.bin" % self.prefix, text)
        self.assertEqual(text.count("port lost (the device is gone)"), 2)
        self.assertIn("STALL: no packet this second", text)
        self.assertIn("no port that opens within", text)
        self.assertRegex(text, r"dev=1\.\d{3} n=\d+ V min/mean/max 4\.\d{3} 4\.\d{3} 4\.\d{3} gaps=0")

    def test_no_port_at_all(self):
        self.assertEqual(self.log(), 1)
        self.assertFalse(os.path.exists("%s-1.bin" % self.prefix))

    def test_max_ends_the_log(self):
        self.ports = [FakePort([None] * 10 ** 6)]
        self.assertEqual(self.log(max_seconds=0.2, interval=1), 0)
        self.assertIn("done after 0 s logged", self.lines[-1])

    def test_without_a_backend_it_asks_for_pyserial(self):
        printed = []
        with mock.patch.object(pico_serial, "backend", lambda: None), \
                mock.patch("builtins.print", lambda *a, **k: printed.append(a[0])):
            self.assertEqual(pico_vlog.main([self.prefix]), 1)
            self.assertEqual(pico_dlog.main([self.prefix]), 1)
        self.assertEqual(printed, [pico_serial.NEEDS_PYSERIAL] * 2)


class Summary(unittest.TestCase):
    def test_volts_and_gaps(self):
        summary = pico_vlog.Summary()
        for ts, volts in ((1000, [3.30, 3.31]), (2000, [3.29]), (5000, [3.35])):
            summary.add(ts, volts)
        self.assertEqual((summary.packets, summary.gaps), (3, 1))
        self.assertAlmostEqual(summary.low, 3.29)
        self.assertAlmostEqual(summary.high, 3.35)
        self.assertIn("dev=0.005 n=3 V min/mean/max 3.290 3.312 3.350 gaps=1", summary.line())
        summary.reset()
        self.assertIn("STALL", summary.line())
        self.assertEqual(summary.gaps, 1)  # kept until the port is reopened


class DebugEvents(unittest.TestCase):
    def lines_for(self, data, quiet_fusb=False):
        lines = []
        on_item = pico_dlog.printer(lines.append, quiet_fusb)
        for item in pico_stream.Stream().feed(data):
            on_item(item)
        return lines

    def test_events_and_command_answers_are_printed(self):
        lines = self.lines_for(bench_stream(packets=2000))
        self.assertEqual(sum("SET_PPS 3700 mV 1000 mA" in line for line in lines), 3)
        self.assertTrue(any(line.endswith("CMD answer to RANGE_GET #7: OK 02") for line in lines))
        self.assertRegex(lines[0], r"^\d\d:\d\d:\d\d +0 us  SET_PPS")

    def test_quiet_fusb_leaves_out_the_interrupts(self):
        frame = debug_frame(1, 0, bytes([2]) + event(1, 13, 0x04, 0, 0) + event(2, 3))
        self.assertEqual(len(self.lines_for(frame)), 2)
        self.assertEqual(len(self.lines_for(frame, quiet_fusb=True)), 1)

    def test_other_frames_are_described(self):
        lines = self.lines_for(adc_packet(1, [1.0]) + debug_frame(5, 0, bytes(32)))
        self.assertEqual(len(lines), 1)
        self.assertIn("CRASH exc_return", lines[0])


if __name__ == "__main__":
    unittest.main()
