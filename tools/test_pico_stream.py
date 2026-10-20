"""Tests for pico_stream.py on synthetic streams.

The builders here (adc_packet, debug_frame, event, bench_stream) are also used
by the tests of the tools that read the stream.

Run: py tools/test_pico_stream.py
"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pico_stream as st  # noqa: E402

LSB = st.V_PER_LSB


def adc_packet(ts_us, volts, rng=1):
    """A measurement packet as USB_ADC_Packet_t lays it out."""
    body = b"".join(struct.pack("<BHHH", rng, int(round(v / LSB)), 2048, 2050) for v in volts)
    return b"\xaa\x55" + struct.pack("<QB", ts_us, len(volts)) + body.ljust(70, b"\0")


def debug_frame(ftype, seq, payload):
    return b"\xaa\x5d" + bytes([len(payload) + 5, ftype, seq]) + payload


def event(t_us, kind, a=0, b=0, c=0):
    return struct.pack("<IBBHI", t_us, kind, a, b, c)


def bench_level(t):
    """VBUS in volts at device time t: 4.2 V, a step to 3.7 V at 1 s with a
    50 ms ramp, a source hard reset at 3 s (0 V for 0.8 s, 5 V for 0.5 s),
    then 3.7 V again."""
    if t < 1.0:
        return 4.2
    if t < 1.05:
        return 4.2 - (t - 1.0) / 0.05 * 0.5
    if t < 3.0:
        return 3.7
    if t < 3.8:
        return 0.0
    if t < 4.3:
        return 5.0
    return 3.7


def bench_stream(packets=6000, start_us=1000000, skip=()):
    """`packets` 1 ms packets of bench_level, starting at device time start_us,
    with junk first, debug frames and a command answer between packets, and
    the packets numbered in `skip` left out (a gap in the stream)."""
    data = bytearray(b"junk\xaa")
    for n in range(packets):
        if n in skip:
            continue
        t = n / 1000.0
        data += adc_packet(start_us + n * 1000, [bench_level(t + k / 10000.0) for k in range(10)])
        if n == 2500:
            data += b"\x00\x01\x02"
        if n % 700 == 0:
            data += debug_frame(1, n // 700 & 0xFF, bytes([1]) + event(n * 1000, 1, 20, 185))
        if n == 1234:
            data += st.command_frame(0x91, 7, b"\x00\x02")
    return bytes(data)


def items_of(data):
    return list(st.parse(data))


class AdcPackets(unittest.TestCase):
    def test_a_packet_decodes(self):
        volts = [3.3 + 0.01 * k for k in range(10)]
        [(kind, ts, got)] = items_of(adc_packet(123456789012, volts))
        self.assertEqual((kind, ts), ("adc", 123456789012))
        for want, value in zip(volts, got):
            self.assertAlmostEqual(want, value, delta=LSB / 2)

    def test_the_firmware_conversion(self):
        # vol_adc 4095 is 3.0 V at the ADC, 33 V at the input.
        [(_, _, [volts])] = items_of(b"\xaa\x55" + struct.pack("<QB", 0, 1)
                                     + struct.pack("<BHHH", 1, 4095, 0, 0) + bytes(63))
        self.assertAlmostEqual(volts, 33.0)

    def test_header_bytes_inside_a_packet_do_not_split_it(self):
        ts = 0x5DAA55AA  # the timestamp holds AA 55 and AA 5D
        self.assertEqual([i[:2] for i in items_of(adc_packet(ts, [1.0] * 10) * 2)],
                         [("adc", ts)] * 2)

    def test_bad_count_or_range_is_skipped(self):
        good = adc_packet(5000, [4.2] * 10)
        bad_count = bytearray(good)
        bad_count[10] = 11
        bad_range = adc_packet(4000, [4.2] * 10, rng=0)
        self.assertEqual([i[1] for i in items_of(bytes(bad_count) + bad_range + good)], [5000])

    def test_a_packet_cut_short_is_dropped(self):
        # A lost USB transfer leaves the first `cut` bytes of a packet, then
        # the next packet. The timestamps' third byte (0x0F) is not 1-3; with
        # one that is, a cut after 7 bytes gets through as a packet.
        first = adc_packet(1000000, [4.2] * 10)
        lost = adc_packet(1001000, [4.1] * 10)
        after = adc_packet(1002000, [4.0] * 10) + adc_packet(1003000, [3.9] * 10)
        for cut in range(1, st.ADC_SIZE):
            got = [i[1] for i in items_of(first + lost[:cut] + after)]
            self.assertEqual(got, [1000000, 1002000, 1003000], "cut after %d bytes" % cut)


class OtherFrames(unittest.TestCase):
    def test_frames_come_out_in_order(self):
        events = bytes([2]) + event(10, 6, 0, 0x01A1, 0) + event(20, 1, 20, 165)
        answer = st.command_frame(0x91, 3, b"\x00\x01")
        data = (adc_packet(1000, [3.3] * 10) + debug_frame(1, 9, events) + answer
                + adc_packet(2000, [3.3] * 10))
        got = items_of(data)
        self.assertEqual([i[0] for i in got], ["adc", "dbg", "cmd", "adc"])
        self.assertEqual(got[1], ("dbg", 1, 9, events))
        self.assertEqual(got[2], ("cmd", 0x91, 3, b"\x00\x01"))

    def test_debug_length_outside_5_to_58_is_skipped(self):
        frame = debug_frame(2, 0, bytes(40))
        short = b"\xaa\x5d\x04\x01\x00"
        long = b"\xaa\x5d\x3b" + bytes(60)
        self.assertEqual(items_of(short + long + frame), [("dbg", 2, 0, bytes(40))])

    def test_command_with_a_wrong_crc_is_skipped(self):
        frame = bytearray(st.command_frame(0x95, 1))
        frame[-1] ^= 0xFF
        too_long = b"\xa5\x5a\x01\x11\x00\x00\x31\x00" + bytes(60)
        good = st.command_frame(0x92, 2, b"\x00")
        self.assertEqual(items_of(bytes(frame) + too_long + good), [("cmd", 0x92, 2, b"\x00")])

    def test_crc_check_value(self):
        self.assertEqual(st.crc16_ccitt(b"123456789"), 0x29B1)

    def test_the_firmware_update_request(self):
        frame = st.command_frame(st.CMD_FW_UPDATE, 1, st.CMD_KEY)
        body = b"\x01\x15\x01\x00\x02\x00\xc3\x3c"
        self.assertEqual(frame, b"\xa5\x5a" + body + struct.pack("<H", st.crc16_ccitt(body)))
        with self.assertRaises(ValueError):
            st.command_frame(0x13, 1, bytes(49))


class Pieces(unittest.TestCase):
    def test_any_split_gives_the_same_items(self):
        data = bench_stream(packets=60)
        whole = items_of(data)
        self.assertEqual(sum(1 for i in whole if i[0] == "adc"), 60)
        for size in (1, 2, 3, 7, 37, 81, 82, 500):
            stream = st.Stream()
            got = []
            for start in range(0, len(data), size):
                got += stream.feed(data[start:start + size])
            self.assertEqual(got, whole, "pieces of %d bytes" % size)

    def test_parse_returns_where_an_incomplete_frame_starts(self):
        packet = adc_packet(1000, [1.0] * 10)
        frames = st.parse(b"xyz" + packet + packet[:50])
        self.assertEqual(next(frames)[0], "adc")
        with self.assertRaises(StopIteration) as stop:
            next(frames)
        self.assertEqual(stop.exception.value, 3 + len(packet))

    def test_a_trailing_header_byte_is_kept(self):
        stream = st.Stream()
        packet = adc_packet(1000, [1.0] * 10)
        self.assertEqual(stream.feed(b"\x00\x00" + packet[:1]), [])
        self.assertEqual(len(stream.feed(packet[1:])), 1)
        self.assertEqual(len(stream.buf), 0)

    def test_the_long_bench_stream(self):
        stream = st.Stream()
        data = bench_stream()
        kinds = {}
        for start in range(0, len(data), 4093):
            for item in stream.feed(data[start:start + 4093]):
                kinds[item[0]] = kinds.get(item[0], 0) + 1
        self.assertEqual(kinds, {"adc": 6000, "dbg": 9, "cmd": 1})


class Describe(unittest.TestCase):
    def test_events(self):
        payload = bytes([3]) + event(5, 1, 20, 165) + event(6, 14, 0, 3, 2) + event(7, 99, 1, 2, 3)
        lines = [st.describe(kind, a, b, c) for _, kind, a, b, c in st.events(payload)]
        self.assertEqual(lines, ["SET_PPS 3300 mV 1000 mA", "KEY B click (btn 2)",
                                 "K99 a=1 b=2 c=3"])

    def test_a_pps_request(self):
        # Request, 1 object, message id 2; RDO object position 5, 3.30 V, 1 A.
        rdo = 5 << 28 | 165 << 9 | 20
        text = st.describe(4, 0, 0x1000 | 2 << 9 | 2, rdo)
        self.assertEqual(text, "TX_REQUEST Request id2 [flagged] (pos 5, 3300 mV, 1000 mA)")

    def test_a_count_larger_than_the_frame_is_cut(self):
        self.assertEqual(len(list(st.events(bytes([4]) + event(1, 2)))), 1)
        self.assertEqual(list(st.events(b"")), [])

    def test_frames(self):
        summary = struct.pack("<IIBBBBIIIIIII", 12, 0x24000000, 1, 1, 0, 0, 11, 5000, 40, 0,
                              3000, 9000, 8000)
        self.assertIn("previous crash: stack overflow", st.describe_frame(2, summary))
        self.assertIn("heap free 9000 B", st.describe_frame(2, summary))
        crash = struct.pack("<II16sIIII", 1, 77, b"defaultTask", 0, 0x50, 1, 200)
        self.assertIn("in task 'defaultTask'", st.describe_frame(3, crash))
        ring = struct.pack("<HB", 252, 2) + event(1, 2) + event(2, 3)
        self.assertEqual(st.describe_frame(6, ring).splitlines()[1],
                         "CRASHLOG 253           2 us  PD_STOP a=0 b=0 c=0")
        self.assertIn("too short", st.describe_frame(4, bytes(10)))

    def test_commands(self):
        self.assertEqual(st.describe_command(0x91, 3, b"\x00\x01"), "CMD answer to RANGE_GET #3: OK 01")
        self.assertEqual(st.describe_command(0x15, 1, st.CMD_KEY), "CMD FW_UPDATE #1 c33c")


if __name__ == "__main__":
    unittest.main()
