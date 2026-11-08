"""Tests for pico_cal.py against a simulated unit and synthetic logs; no unit needed.

Run: py tools/test_pico_cal.py
"""

import contextlib
import csv
import io
import json
import os
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pico_cal  # noqa: E402
import pico_serial  # noqa: E402
import pico_stream  # noqa: E402
from test_pico_stream import debug_frame, event  # noqa: E402

FIELDS = pico_cal.FIELDS
# The CAL_GET answer the unit gave on 2026-11-09 before it was calibrated, and what it
# was given then
BEFORE_FRAME = bytes.fromhex("a5 5a 01 90 01 01 19 00 00 00 00 80 3f 00 00 00 00 00 00 80 3f"
                             " 00 00 00 00 00 00 80 3f 00 00 00 00 42 d3")
AFTER = {"low_gain": 1.0, "low_offset_ua": 0.0, "mid_gain": 1.0, "mid_offset_ua": -67.7,
         "high_gain": 0.9527, "high_offset_ua": -18113.0}


def as_f32(cal):
    return [pico_cal.f32(cal[k]) for k in FIELDS]


def packet(ts_us, rows):
    """A measurement packet with these (range, vol_adc, cur_adc, ref_adc) samples."""
    body = b"".join(struct.pack("<BHHH", *row) for row in rows)
    return b"\xaa\x55" + struct.pack("<QB", ts_us, len(rows)) + body.ljust(70, b"\0")


def expected_ua(rng, vol, cur, ref, gain, offset):
    """The firmware's formula written out, apart from pico_cal's constants."""
    shunt = {1: 50.0, 2: 0.5, 3: 0.005}[rng]
    return ((cur - ref) * 3.0 / 4096 / 50 / shunt * 1e6 * gain + offset
            - vol * 3.0 / 4095 * 11 / 1.1e6 * 1e6)


MID = [(2, 459, 2048 + 35 + k % 3 - 1, 2048) for k in range(10)]  # deltas 34-36 LSB
HIGH = [(3, 459, 2048 + 13 + k % 2, 2048) for k in range(10)]  # deltas 13-14 LSB


def bench_log():
    """A log as pico_vlog.py writes one: junk, a stale first packet at 0.2 s, then 100
    packets from 10.000 s, 50 on mid and 50 on high, with no packet at 10.050 s (the
    1 ms the firmware skips after a range switch), a command answer and a debug frame
    between packets, and one sample whose ref_adc is outside the client's filter."""
    data = bytearray(b"\x00\xaa\x55junk")
    data += packet(200000, MID)
    for n in range(101):
        if n == 50:
            continue
        rows = list(MID if n < 50 else HIGH)
        if n == 20:
            rows[3] = (2, 459, 2200 + 35, 2200)
        data += packet(10000000 + n * 1000, rows)
        if n == 30:
            data += pico_stream.command_frame(0x91, 7, b"\x00\x00\x02")
        if n == 70:
            data += debug_frame(1, 1, bytes([1]) + event(70000, 1, 20, 185))
    return bytes(data)


class FakeUnit:
    """Answers commands as User/user_CmdStrategy.c does, and streams a packet of MID
    samples every millisecond."""

    def __init__(self, cal=None, streaming=True):
        self.cal = as_f32(cal or pico_cal.DEFAULTS)
        self.mode, self.in_use = 0, 2
        self.streaming = streaming
        self.out = bytearray()
        self.received = bytearray()
        self.commands = []
        self.parser = pico_stream.Stream()
        self.start = time.monotonic()
        self.sent = 0

    def read(self, n):
        if self.streaming:
            due = int((time.monotonic() - self.start) * 1000) + 1
            while self.sent < due:
                self.out += packet(1000000 + self.sent * 1000, MID)
                self.sent += 1
        data = bytes(self.out[:n])
        del self.out[:n]
        return data

    def write(self, data):
        self.received += data
        for item in self.parser.feed(data):
            if item[0] != "cmd":
                continue
            _, ftype, seq, payload = item
            self.commands.append(ftype)
            if ftype == 0x10:
                answer = b"\x00" + struct.pack("<6f", *self.cal)
            elif ftype == 0x11:
                answer = bytes([0, self.mode, self.in_use])
            elif ftype == 0x12:
                self.mode = payload[0]
                self.in_use = payload[0] or self.in_use
                answer = bytes([0, self.mode, self.in_use])
            elif ftype == 0x13:
                new = list(struct.unpack("<6f", payload))
                if pico_cal.problems(dict(zip(FIELDS, new))):
                    answer = b"\x04"
                else:
                    self.cal = new
                    answer = b"\x00" + payload
            else:
                answer = b"\x02"
            self.out += pico_stream.command_frame(ftype | 0x80, seq, answer)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.out.clear()

    def close(self):
        pass


@contextlib.contextmanager
def attached(unit):
    with mock.patch.object(pico_serial, "open_port", lambda name, timeout=0.05: unit), \
            mock.patch.object(pico_serial, "backend", lambda: "fake"), \
            mock.patch.object(pico_cal, "LOOK_S", 0.02):
        yield


def run(*argv):
    """(exit status, printed text) of pico_cal.main(argv)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        status = pico_cal.main(list(argv))
    return status, out.getvalue()


class Calibration(unittest.TestCase):
    def test_the_scales(self):
        self.assertAlmostEqual(pico_cal.SCALE[1], 0.29296875, places=12)
        self.assertAlmostEqual(pico_cal.SCALE[2], 29.296875, places=10)
        self.assertAlmostEqual(pico_cal.SCALE[3], 2929.6875, places=8)

    def test_the_answer_before_calibration_holds_the_defaults(self):
        [(kind, ftype, seq, payload)] = list(pico_stream.parse(BEFORE_FRAME))
        self.assertEqual((kind, ftype, seq, payload[0]), ("cmd", 0x90, 0x101, 0))
        self.assertEqual(pico_cal.decode_cal(payload[1:]), pico_cal.DEFAULTS)

    def test_the_payload_round_trips_as_float32(self):
        payload = pico_cal.encode_cal(AFTER)
        self.assertEqual(len(payload), 24)
        self.assertEqual([pico_cal.decode_cal(payload)[k] for k in FIELDS], as_f32(AFTER))

    def test_the_firmware_check(self):
        self.assertEqual(pico_cal.problems(AFTER), [])
        self.assertEqual(pico_cal.problems(pico_cal.DEFAULTS), [])
        for change, word in (({"mid_gain": 1.3}, "mid gain"), ({"low_gain": 0.7}, "low gain"),
                             ({"mid_offset_ua": 33 * 29.296875}, "mid offset"),
                             ({"high_offset_ua": float("nan")}, "high offset"),
                             ({"high_gain": float("inf")}, "high gain")):
            cal = dict(AFTER, **change)
            [problem] = pico_cal.problems(cal)
            self.assertIn(word, problem)
        self.assertEqual(pico_cal.problems(dict(AFTER, mid_offset_ua=-31 * 29.296875)), [])

    def test_load_cal(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = os.path.join(tmp, "saved.json")
            with open(saved, "w", encoding="utf-8") as fh:
                json.dump({"read_at": "x", "calibration": AFTER}, fh)
            self.assertEqual(pico_cal.load_cal(saved), AFTER)
            bare = os.path.join(tmp, "bare.json")
            with open(bare, "w", encoding="utf-8") as fh:
                json.dump(dict(AFTER, high_offset_ua=-18113), fh)
            self.assertEqual(pico_cal.load_cal(bare), AFTER)
            short = os.path.join(tmp, "short.json")
            with open(short, "w", encoding="utf-8") as fh:
                json.dump({"calibration": {"mid_gain": 1.0}}, fh)
            with self.assertRaises(pico_cal.CalError):
                pico_cal.load_cal(short)


class Formula(unittest.TestCase):
    def test_a_mid_sample(self):
        self.assertAlmostEqual(pico_cal.current_ua((2, 459, 2083, 2048), AFTER),
                               expected_ua(2, 459, 2083, 2048, 1.0, -67.7), places=9)

    def test_a_high_sample(self):
        self.assertAlmostEqual(pico_cal.current_ua((3, 459, 2061, 2048), AFTER),
                               expected_ua(3, 459, 2061, 2048, 0.9527, -18113.0), places=6)

    def test_a_low_sample_and_the_divider(self):
        # No current through the shunt: what is left is the divider's V / 1.1 MOhm.
        v = 459 * 3.0 / 4095 * 11
        self.assertAlmostEqual(pico_cal.current_ua((1, 459, 2048, 2048), AFTER), -v / 1.1,
                               places=9)


class Logs(unittest.TestCase):
    def setUp(self):
        self.data = bench_log()

    def test_the_window(self):
        w = pico_cal.window_of(self.data)
        self.assertAlmostEqual(w.stale, 0.2)
        self.assertEqual(w.packets, 100)
        self.assertEqual(w.count, {1: 0, 2: 500, 3: 500})
        self.assertEqual((w.gaps, w.outside_ref), (1, 1))
        self.assertAlmostEqual(w.missing, 0.001)
        self.assertAlmostEqual(w.span(), 0.101)
        self.assertAlmostEqual(w.vbus(), 459 * 3.0 / 4095 * 11)

    def test_the_mean_is_the_mean_of_the_corrected_samples(self):
        stale, packets = pico_cal.log_packets(self.data)
        rows = list(pico_cal.corrected(packets, AFTER))
        self.assertEqual(len(rows), 1000)
        for t, k, sample, i_ua in rows[::97]:
            self.assertEqual(i_ua, pico_cal.current_ua(sample, AFTER))
            self.assertAlmostEqual(i_ua, expected_ua(*(sample + (
                AFTER[pico_cal.RANGES[sample[0]] + "_gain"],
                AFTER[pico_cal.RANGES[sample[0]] + "_offset_ua"]))), places=6)
        w = pico_cal.window_of(self.data)
        mean = sum(row[3] for row in rows) / len(rows)
        self.assertAlmostEqual(w.mean_ua(AFTER), mean, places=6)
        self.assertAlmostEqual(w.charge_uc(AFTER), mean * 0.101, places=6)
        mid = [row[3] for row in rows if row[2][0] == 2]
        self.assertAlmostEqual(w.mean_ua(AFTER, (2,)), sum(mid) / len(mid), places=6)

    def test_the_calibration_moves_the_mean(self):
        w = pico_cal.window_of(self.data, 10.0, 10.0495)  # the mid half
        raw = w.mean_ua(pico_cal.DEFAULTS)
        self.assertAlmostEqual(w.mean_ua(AFTER), raw - 67.7, places=6)
        w = pico_cal.window_of(self.data, 10.051)  # the high half
        delta = 13.5 * 2929.6875
        divider = 459 * 3.0 / 4095 * 11 / 1.1
        self.assertAlmostEqual(w.mean_ua(pico_cal.DEFAULTS), delta - divider, places=6)
        self.assertAlmostEqual(w.mean_ua(AFTER), delta * 0.9527 - 18113 - divider, places=6)

    def test_from_and_to(self):
        w = pico_cal.window_of(self.data, 10.0105, 10.0195)
        self.assertEqual((w.packets, round(w.first, 6), round(w.last, 6)), (9, 10.011, 10.019))
        self.assertEqual(w.gaps, 0)
        self.assertAlmostEqual(w.span(), 0.009)

    def test_a_first_packet_close_to_the_second_is_kept(self):
        w = pico_cal.window_of(packet(9999000, MID) + packet(10000000, MID))
        self.assertIsNone(w.stale)
        self.assertEqual(w.packets, 2)

    def test_an_empty_window(self):
        w = pico_cal.window_of(self.data, 20.0)
        self.assertEqual((w.packets, w.span()), (0, 0.0))
        self.assertNotEqual(w.mean_ua(AFTER), w.mean_ua(AFTER))  # nan


class Commands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_read_saves_what_it_read(self):
        unit = FakeUnit(AFTER)
        with attached(unit):
            status, text = run("read", "--port", "FAKE", "--save", self.path("cal.json"))
            self.assertEqual(status, 0, text)
            self.assertIn("validity check: passes", text)
            with open(self.path("cal.json"), encoding="utf-8") as fh:
                record = json.load(fh)
            self.assertEqual([record["calibration"][k] for k in FIELDS], as_f32(AFTER))
            self.assertEqual((record["range_mode"], record["range_in_use"]), (0, 2))
            status, text = run("read", "--port", "FAKE", "--save", self.path("cal.json"))
            self.assertEqual(status, 1)  # never overwrites a file
        self.assertEqual(unit.commands, [0x10, 0x11, 0x10, 0x11])

    def test_measure(self):
        unit = FakeUnit(AFTER)
        with attached(unit):
            status, text = run("measure", "--port", "FAKE", "--seconds", "0.05")
        self.assertEqual(status, 0, text)
        self.assertEqual(unit.commands, [0x10])
        raw = expected_ua(2, 459, 2048 + 34.9, 2048, 1.0, 0.0)
        self.assertIn("gain 1, offset 0: %.3f uA" % raw, text)
        self.assertIn("with the unit's calibration: %.3f uA" % (raw - 67.7), text)

    def test_set_without_yes_sends_nothing(self):
        unit = FakeUnit()
        with attached(unit):
            status, text = run("set", "--port", "FAKE", "--mid-offset", "-67.7")
        self.assertEqual(status, 0, text)
        self.assertIn("Not sent", text)
        self.assertEqual(unit.commands, [0x10])
        self.assertEqual(unit.cal, as_f32(pico_cal.DEFAULTS))

    def test_set_writes_and_reads_back(self):
        unit = FakeUnit()
        with attached(unit):
            status, text = run("set", "--port", "FAKE", "--mid-offset", "-67.7", "--high-gain",
                               "0.9527", "--high-offset", "-18113", "--yes")
        self.assertEqual(status, 0, text)
        self.assertEqual(unit.cal, as_f32(AFTER))
        self.assertEqual(unit.commands, [0x10, 0x13, 0x10])

    def test_values_the_firmware_would_refuse_are_never_sent(self):
        unit = FakeUnit()
        with attached(unit):
            for flags in (["--mid-offset", "-2000"], ["--high-gain", "1.3"]):
                status, text = run("set", "--port", "FAKE", "--yes", *flags)
                self.assertEqual(status, 1)
                self.assertIn("Not sent", text)
        self.assertNotIn(0x13, unit.commands)

    def test_restore(self):
        with open(self.path("after.json"), "w", encoding="utf-8") as fh:
            json.dump({"calibration": AFTER}, fh)
        unit = FakeUnit()
        with attached(unit):
            status, text = run("restore", self.path("after.json"), "--port", "FAKE")
            self.assertEqual((status, unit.cal), (0, as_f32(pico_cal.DEFAULTS)))
            status, text = run("restore", self.path("after.json"), "--port", "FAKE", "--yes")
        self.assertEqual(status, 0, text)
        self.assertEqual(unit.cal, as_f32(AFTER))

    def test_range(self):
        unit = FakeUnit()
        with attached(unit):
            status, text = run("range", "high", "--port", "FAKE")
        self.assertEqual(status, 0, text)
        self.assertEqual((unit.mode, unit.commands), (3, [0x12]))

    def test_a_port_without_packets_is_never_written(self):
        unit = FakeUnit(streaming=False)
        with attached(unit):
            for argv in (["read"], ["set", "--mid-offset", "-67.7", "--yes"], ["range", "high"]):
                status, text = run(*(argv + ["--port", "FAKE"]))
                self.assertEqual(status, 1)
                self.assertIn("Nothing was sent", text)
        self.assertEqual(unit.received, b"")

    def test_no_port_found(self):
        with mock.patch.object(pico_serial, "backend", lambda: "fake"), \
                mock.patch.object(pico_serial, "list_ports", lambda: []):
            status, text = run("read")
        self.assertEqual(status, 1)
        self.assertIn("Found 0 ports", text)

    def write_log(self):
        with open(self.path("run-1.bin"), "wb") as fh:
            fh.write(bench_log())
        with open(self.path("cal.json"), "w", encoding="utf-8") as fh:
            json.dump({"calibration": AFTER}, fh)
        return self.path("run-1.bin")

    def test_correct_from_a_file(self):
        log = self.write_log()
        status, text = run("correct", log, "--cal", self.path("cal.json"),
                           "--csv", self.path("out.csv"))
        self.assertEqual(status, 0, text)
        w = pico_cal.window_of(bench_log())
        self.assertIn("%-17s mean %.3f uA, charge %.3f uC"
                      % ("Calibrated", w.mean_ua(AFTER), w.charge_uc(AFTER)), text)
        self.assertIn("Dropped a stale first packet at 0.200 s", text)
        self.assertIn("Gaps over 1.5 ms: 1, 1.0 ms", text)
        with open(self.path("out.csv"), newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], ["t_s", "k", "range", "vol_adc", "cur_adc", "ref_adc",
                                   "current_ua"])
        self.assertEqual(len(rows), 1001)
        self.assertEqual(rows[1][:6], ["10.000000", "0", "2", "459", "2082", "2048"])
        self.assertAlmostEqual(float(rows[1][6]), expected_ua(2, 459, 2082, 2048, 1.0, -67.7),
                               places=3)
        status, text = run("correct", log, "--cal", self.path("cal.json"),
                           "--csv", self.path("out.csv"))
        self.assertEqual(status, 1)  # never overwrites a file

    def test_correct_from_the_unit_only_reads(self):
        log = self.write_log()
        unit = FakeUnit(AFTER)
        with attached(unit):
            status, text = run("correct", log, "--unit", "--port", "FAKE", "--from", "10.051")
        self.assertEqual(status, 0, text)
        self.assertEqual(unit.commands, [0x10])
        w = pico_cal.window_of(bench_log(), 10.051)
        self.assertIn("mean %.3f uA" % w.mean_ua(dict(zip(FIELDS, as_f32(AFTER)))), text)
        self.assertIn("50 packets", text)

    def test_correct_needs_a_calibration(self):
        log = self.write_log()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            pico_cal.main(["correct", log])


if __name__ == "__main__":
    unittest.main()
