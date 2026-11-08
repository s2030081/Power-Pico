#!/usr/bin/env python3
"""Read, measure with, change and apply the PowerPico's current calibration.

Usage:
  py tools/pico_cal.py read    [--save FILE]             CAL_GET and RANGE_GET; writes nothing
  py tools/pico_cal.py measure [--seconds S]             averages the live stream; writes nothing
  py tools/pico_cal.py range   auto|low|mid|high         RANGE_SET, kept until a power cycle
  py tools/pico_cal.py set     [--mid-offset UA] [--high-gain G] ... [--yes]
                                                         CAL_SET, saved in the EEPROM at once
  py tools/pico_cal.py restore FILE [--yes]              CAL_SET from a file read --save wrote
  py tools/pico_cal.py correct LOG.bin (--cal FILE | --unit) [--from S] [--to S] [--csv OUT]
                                                         applies a calibration to a pico_vlog.py log

The commands that use the unit take --port PORT (default: the only port with the
PowerPico's USB IDs). Each reads the port for 1.5 s first and sends nothing to a port
that does not carry the firmware's measurement packets. set and restore print what
they would send and send it only with --yes. Both refuse what the firmware's own
check (ADC_Calibration_IsValid: each gain 0.75-1.25, each offset 32 LSB or less)
would refuse, in either firmware: 1.1.7 counts the 32 LSB on the calibrated scale
(SCALE x gain) and 1.2.0 on the nominal one (SCALE), so this takes the smaller.

set and restore then check the CAL_SET echo and a fresh CAL_GET against what they
sent. Firmware 1.2.0 keeps each gain as gain x SCALE in float32 and divides it back
for the answer, so a gain can come back one float32 step (about 1e-7) from what was
sent; they accept that and say so. Anything more, and any offset that differs, fails.

The firmware (User/user_AdcDataStrategy.c) converts a sample as

  I_uA = (cur_adc - ref_adc) * SCALE[range] * gain[range] + offset_uA[range] - V / 1.1 MOhm

with SCALE = 3 / 4096 / 50 / R_shunt * 1e6 uA per LSB (R_shunt 50, 0.5 and 0.005 ohm on
the low, mid and high ranges) and V = vol_adc * 3 / 4095 * 11. The last term is the
current drawn by the 1 M / 100 k divider that reads VBUS. The six calibration values
are kept in the BL24C02 EEPROM and loaded at boot; CAL_SET saves them at once. Only the
unit's screen uses them: the stream carries raw ADC codes, and client 0.1.3 converts
them with gain 1 and offset 0. Its recordings also drop the range byte, so they cannot
be corrected exactly. pico_vlog.py's .bin logs keep it.

correct applies a calibration to each sample of such a log whose packet's timestamp is
within --from and --to (device seconds). The calibration comes from a file read --save
wrote (--cal) or from the unit by CAL_GET (--unit). It prints the samples on each
range, the mean current with the calibration and without it (gain 1, offset 0), and
the charge: the mean times the time the packets cover, 1 ms each, so stream gaps count
at the mean current. In auto the firmware cuts short the packet in which it switches
range and sends none for the next 1 ms. --csv writes each sample's current. This
computes in double and the firmware in float32; the difference is far below one LSB.
"""

import argparse
import contextlib
import csv
import itertools
import json
import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I leaves it out
import pico_serial  # noqa: E402
import pico_stream  # noqa: E402

CAL_GET, RANGE_GET, RANGE_SET, CAL_SET = 0x10, 0x11, 0x12, 0x13
RANGES = {1: "low", 2: "mid", 3: "high"}
MODES = {"auto": 0, "low": 1, "mid": 2, "high": 3}
SHUNT_OHMS = {1: 50.0, 2: 0.5, 3: 0.005}
SCALE = {r: 3.0 / 4096.0 / 50.0 / SHUNT_OHMS[r] * 1e6 for r in SHUNT_OHMS}  # uA per LSB
# The six floats of the CAL_GET and CAL_SET payloads, in their order (user_CmdStrategy.c)
FIELDS = ["low_gain", "low_offset_ua", "mid_gain", "mid_offset_ua", "high_gain", "high_offset_ua"]
DEFAULTS = {"low_gain": 1.0, "low_offset_ua": 0.0, "mid_gain": 1.0, "mid_offset_ua": 0.0,
            "high_gain": 1.0, "high_offset_ua": 0.0}
DIVIDER_UA_PER_LSB = pico_stream.V_PER_LSB / 1.1  # what the 1.1 MOhm divider draws, per vol_adc LSB
CLIENT_REF = (1998, 2098)  # client 0.1.3 drops samples whose ref_adc is outside this
PACKET_S = 0.001  # ten samples at 10 kHz
GAP_S = 0.0015  # a longer step between packets is a gap, as in pico_vtrace
LOOK_S = 1.5  # how long a port is read before anything is sent to it


class CalError(Exception):
    pass


# --- the calibration -----------------------------------------------------------

def f32(x):
    """x as the float32 the firmware keeps."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def problems(cal):
    """What either firmware's ADC_Calibration_IsValid would refuse in `cal`; empty when
    both would take it. The offset limit is 32 LSB of SCALE x gain in 1.1.7 and of
    SCALE in 1.2.0, so it is 32 LSB of SCALE x min(gain, 1) here."""
    found = []
    for r, name in RANGES.items():
        gain, offset = cal[name + "_gain"], cal[name + "_offset_ua"]
        if not math.isfinite(gain) or not 0.75 <= gain <= 1.25:
            found.append("%s gain %r is outside 0.75-1.25" % (name, gain))
            continue
        limit = 32.0 * SCALE[r] * min(gain, 1.0)
        if not math.isfinite(offset) or abs(offset) > limit:
            found.append("%s offset %r uA is over 32 LSB (%.6g uA)" % (name, offset, limit))
    return found


def f32_steps(a, b):
    """How many float32 values apart a and b are."""
    def ordered(x):
        i = struct.unpack("<i", struct.pack("<f", x))[0]
        return i if i >= 0 else -(i & 0x7FFFFFFF)
    return abs(ordered(a) - ordered(b))


def decode_cal(payload):
    """The calibration in the 24 bytes after a CAL_GET or CAL_SET answer's status byte."""
    return dict(zip(FIELDS, struct.unpack("<6f", payload[:24])))


def encode_cal(cal):
    return struct.pack("<6f", *[cal[k] for k in FIELDS])


def load_cal(path):
    """The calibration in a file read --save wrote, or in a JSON object of the six values."""
    try:
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
    except ValueError as error:
        raise CalError("%s: not JSON (%s)" % (path, error))
    cal = record.get("calibration", record) if isinstance(record, dict) else None
    if not isinstance(cal, dict):
        raise CalError("%s holds no calibration" % path)
    missing = [k for k in FIELDS if not isinstance(cal.get(k), (int, float))]
    if missing:
        raise CalError("%s: no number for %s" % (path, ", ".join(missing)))
    return {k: float(cal[k]) for k in FIELDS}


def show_cal(cal, title):
    print(title)
    for r, name in RANGES.items():
        gain, offset = cal[name + "_gain"], cal[name + "_offset_ua"]
        lsb = offset / (SCALE[r] * gain) if gain else float("nan")
        print("  %-4s gain %.9g  offset %.9g uA  (%.3f LSB of %.6g uA)"
              % (name, gain, offset, lsb, SCALE[r]))


def current_ua(sample, cal):
    """The firmware's current for one (range, vol_adc, cur_adc, ref_adc) sample, in uA."""
    rng, vol, cur, ref = sample
    name = RANGES[rng]
    return ((cur - ref) * SCALE[rng] * cal[name + "_gain"] + cal[name + "_offset_ua"]
            - vol * DIVIDER_UA_PER_LSB)


# --- logged streams ------------------------------------------------------------

def log_packets(data, t0=None, t1=None):
    """(stale, packets) for a stream file's bytes, as pico_vlog.py wrote them.

    packets yields (t_s, samples) for each measurement packet whose timestamp is
    within t0..t1 device seconds, samples being (range, vol_adc, cur_adc, ref_adc)
    tuples. A first packet more than 1 s older than the second is dropped, as
    pico_vtrace drops it; stale is its time, or None.
    """
    packets = ((item[1] / 1e6, item[3]) for item in pico_stream.parse(data, rows=True)
               if item[0] == "adc")
    head = list(itertools.islice(packets, 2))
    stale = None
    if len(head) == 2 and head[1][0] - head[0][0] > 1.0:
        stale = head.pop(0)[0]
    return stale, ((t, samples) for t, samples in itertools.chain(head, packets)
                   if (t0 is None or t >= t0) and (t1 is None or t <= t1))


def corrected(packets, cal):
    """(t_s, k, sample, current_uA) for each sample of `packets` under `cal`, k being
    the sample's place in its packet."""
    for t, samples in packets:
        for k, sample in enumerate(samples):
            yield t, k, sample, current_ua(sample, cal)


class Window:
    """Sums over the samples of a stretch of stream, enough to give its mean current
    under any calibration, and the timing of its packets."""

    def __init__(self, stale=None):
        self.stale = stale
        self.count = dict.fromkeys(RANGES, 0)
        self.delta = dict.fromkeys(RANGES, 0)  # sum of cur_adc - ref_adc
        self.vol = dict.fromkeys(RANGES, 0)  # sum of vol_adc
        self.outside_ref = 0
        self.packets = 0
        self.first = self.last = None
        self.gaps = 0
        self.missing = 0.0

    def add(self, t, samples):
        if self.last is not None and t - self.last > GAP_S:
            self.gaps += 1
            self.missing += t - self.last - PACKET_S
        if self.first is None:
            self.first = t
        self.last = t
        self.packets += 1
        for rng, vol, cur, ref in samples:
            self.count[rng] += 1
            self.delta[rng] += cur - ref
            self.vol[rng] += vol
            if not CLIENT_REF[0] <= ref <= CLIENT_REF[1]:
                self.outside_ref += 1

    def samples(self, ranges=tuple(RANGES)):
        return sum(self.count[r] for r in ranges)

    def span(self):
        """The seconds the packets cover, 1 ms each, gaps included."""
        return self.last - self.first + PACKET_S if self.packets else 0.0

    def vbus(self):
        n = self.samples()
        return sum(self.vol.values()) / n * pico_stream.V_PER_LSB if n else float("nan")

    def mean_ua(self, cal, ranges=tuple(RANGES)):
        """The mean current of the samples on `ranges` under `cal`, in uA."""
        n = self.samples(ranges)
        if not n:
            return float("nan")
        total = 0.0
        for r in ranges:
            name = RANGES[r]
            total += (self.delta[r] * SCALE[r] * cal[name + "_gain"]
                      + self.count[r] * cal[name + "_offset_ua"]
                      - self.vol[r] * DIVIDER_UA_PER_LSB)
        return total / n

    def charge_uc(self, cal):
        """The mean current times the span, in microcoulombs."""
        return self.mean_ua(cal) * self.span()


def window_of(data, t0=None, t1=None):
    """The Window over a stream file's packets within t0..t1 device seconds."""
    stale, packets = log_packets(data, t0, t1)
    window = Window(stale)
    for t, samples in packets:
        window.add(t, samples)
    return window


# --- the unit ------------------------------------------------------------------

class Unit:
    """The PowerPico on a serial port: its stream and its commands."""

    def __init__(self, name):
        self.name = name
        self.port = pico_serial.open_port(name, timeout=0.05)
        self.seq = 0x100

    def close(self):
        try:
            self.port.close()
        except OSError:
            pass

    def read_for(self, seconds):
        """What the port sends in `seconds`."""
        stop = time.monotonic() + seconds
        data = bytearray()
        while time.monotonic() < stop:
            data += self.port.read(4096)
        return bytes(data)

    def check_running(self):
        packets = sum(1 for item in pico_stream.parse(self.read_for(LOOK_S)) if item[0] == "adc")
        if packets < 3:
            raise CalError("%s: %d measurement packets in %.1f s, so this is not the running "
                           "PowerPico firmware. Nothing was sent." % (self.name, packets, LOOK_S))

    def command(self, ftype, payload=b"", timeout=2.0):
        """(the frame sent, the answer's frame, the answer's payload) of one command."""
        self.seq += 1
        frame = pico_stream.command_frame(ftype, self.seq, payload)
        if hasattr(self.port, "reset_input_buffer"):
            self.port.reset_input_buffer()
        self.port.write(frame)
        if hasattr(self.port, "flush"):
            self.port.flush()
        stream = pico_stream.Stream()
        stop = time.monotonic() + timeout
        while time.monotonic() < stop:
            for item in stream.feed(self.port.read(4096)):
                if item[0] == "cmd" and item[1] == ftype | 0x80 and item[2] == self.seq:
                    return frame, pico_stream.command_frame(item[1], item[2], item[3]), item[3]
        raise CalError("%s: no answer to %s #%d within %.1f s"
                       % (self.name, pico_stream.CMD_NAMES[ftype], self.seq, timeout))


def open_unit(name):
    """The Unit on port `name`, or on the only PowerPico port, once it shows the
    running firmware's packets."""
    if pico_serial.backend() is None:
        raise CalError(pico_serial.NEEDS_PYSERIAL)
    if not name:
        ports = pico_serial.list_ports()
        if len(ports) != 1:
            raise CalError("Found %d ports with the PowerPico's USB IDs%s. Name one with --port."
                           % (len(ports), (": " + ", ".join(p.device for p in ports))
                              if ports else ""))
        name = ports[0].device
    unit = Unit(name)
    try:
        unit.check_running()
    except BaseException:
        unit.close()
        raise
    return unit


def status_text(answer):
    if not answer:
        return "an empty answer"
    s = answer[0]
    text = pico_stream.CMD_STATUS[s] if s < len(pico_stream.CMD_STATUS) else "status %d" % s
    return "%s (%d bytes)" % (text, len(answer))


def get_cal(unit, show=False):
    """(calibration, answer frame) from CAL_GET."""
    sent, frame, answer = unit.command(CAL_GET)
    if show:
        print("CAL_GET sent    %s\nCAL_GET answer  %s" % (sent.hex(" "), frame.hex(" ")))
    if len(answer) < 25 or answer[0] != 0:
        raise CalError("CAL_GET: %s" % status_text(answer))
    return decode_cal(answer[1:25]), frame


def get_range(unit):
    """(mode, range in use, answer frame) from RANGE_GET."""
    _, frame, answer = unit.command(RANGE_GET)
    if len(answer) < 3 or answer[0] != 0:
        raise CalError("RANGE_GET: %s" % status_text(answer))
    return answer[1], answer[2], frame


def write_cal(unit, new):
    """CAL_SET `new`, then check the echo and a fresh CAL_GET against it."""
    found = problems(new)
    if found:
        raise CalError("Not sent: " + "; ".join(found))
    sent, frame, answer = unit.command(CAL_SET, encode_cal(new))
    print("CAL_SET sent    %s\nCAL_SET answer  %s" % (sent.hex(" "), frame.hex(" ")))
    if len(answer) < 25 or answer[0] != 0:
        raise CalError("CAL_SET: %s" % status_text(answer))
    echoed = decode_cal(answer[1:25])
    back, _ = get_cal(unit)
    stepped = []
    for k in FIELDS:
        # 1.2.0 keeps gain x SCALE in float32: a gain may come back one step off
        allowed = 1 if k.endswith("_gain") else 0
        if f32_steps(f32(new[k]), echoed[k]) > allowed or echoed[k] != back[k]:
            raise CalError("Read-back differs at %s: sent %r, echoed %r, read %r"
                           % (k, new[k], echoed[k], back[k]))
        if echoed[k] != f32(new[k]):
            stepped.append(k)
    show_cal(back, "Calibration now on the unit (read back):")
    for k in stepped:
        print("  %s came back one float32 step from what was sent (%r for %r): the "
              "firmware keeps gain x SCALE, as 1.2.0 does" % (k, back[k], f32(new[k])))


# --- commands ------------------------------------------------------------------

def cmd_read(args):
    unit = open_unit(args.port)
    try:
        cal, cal_frame = get_cal(unit, show=True)
        mode, in_use, range_frame = get_range(unit)
    finally:
        unit.close()
    print("RANGE_GET answer %s" % range_frame.hex(" "))
    show_cal(cal, "Calibration on the unit (as loaded at boot or by the last CAL_SET):")
    names = {v: k for k, v in MODES.items()}
    print("Range mode %s (%d), range in use %s (%d)"
          % (names.get(mode, "?"), mode, RANGES.get(in_use, "?"), in_use))
    print("All defaults (gain 1, offset 0): %s" % ("yes" if cal == DEFAULTS else "no"))
    found = problems(cal)
    print("The firmware's validity check (1.1.7 and 1.2.0): %s"
          % ("passes" if not found else "; ".join(found)))
    if args.save:
        record = {"read_at": time.strftime("%Y-%m-%d %H:%M:%S"), "port": unit.name,
                  "calibration": cal, "cal_get_answer_frame": cal_frame.hex(" "),
                  "range_mode": mode, "range_in_use": in_use,
                  "range_get_answer_frame": range_frame.hex(" ")}
        with open(args.save, "x", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
        print("Saved to %s" % args.save)


def cmd_measure(args):
    unit = open_unit(args.port)
    try:
        cal, _ = get_cal(unit)
        data = unit.read_for(args.seconds)
    finally:
        unit.close()
    window = Window()
    deltas = {r: [] for r in RANGES}
    for t, samples in log_packets(data)[1]:
        window.add(t, samples)
        for rng, _, cur, ref in samples:
            deltas[rng].append(cur - ref)
    if not window.samples():
        raise CalError("No measurement packets in %.1f s" % args.seconds)
    print("%d samples in %.1f s; VBUS %.4f V, so %.3f uA through the divider is subtracted; "
          "%d with ref_adc outside client 0.1.3's filter (%d-%d), kept as the firmware keeps them"
          % (window.samples(), args.seconds, window.vbus(), window.vbus() / 1.1,
             window.outside_ref, CLIENT_REF[0], CLIENT_REF[1]))
    for r, name in RANGES.items():
        d = deltas[r]
        if not d:
            continue
        m = sum(d) / len(d)
        sd = math.sqrt(sum((x - m) ** 2 for x in d) / len(d))
        print("  %-4s %7d samples  delta %.3f LSB (sd %.2f, %d..%d)  gain 1, offset 0: %.3f uA  "
              "with the unit's calibration: %.3f uA"
              % (name, len(d), m, sd, min(d), max(d), window.mean_ua(DEFAULTS, (r,)),
                 window.mean_ua(cal, (r,))))
    print("All samples: %.3f uA with gain 1 and offset 0; %.3f uA with the unit's calibration "
          "(the screen's formula)" % (window.mean_ua(DEFAULTS), window.mean_ua(cal)))


def cmd_range(args):
    unit = open_unit(args.port)
    try:
        sent, frame, answer = unit.command(RANGE_SET, bytes([MODES[args.mode]]))
    finally:
        unit.close()
    print("RANGE_SET sent   %s\nRANGE_SET answer %s" % (sent.hex(" "), frame.hex(" ")))
    if len(answer) < 3 or answer[0] != 0:
        raise CalError("RANGE_SET: %s" % status_text(answer))
    print("Mode %d, range in use %d (in RAM only: a power cycle returns the saved mode)"
          % (answer[1], answer[2]))


def change_cal(args, changes, title):
    unit = open_unit(args.port)
    try:
        old, _ = get_cal(unit)
        show_cal(old, "Before:")
        new = dict(old)
        new.update(changes)
        show_cal(new, title)
        found = problems(new)
        if found:
            raise CalError("Not sent: " + "; ".join(found))
        if not args.yes:
            print("Not sent: add --yes to write it to the unit's EEPROM.")
            return
        write_cal(unit, new)
    finally:
        unit.close()


def cmd_set(args):
    changes = {k: getattr(args, k) for k in FIELDS if getattr(args, k) is not None}
    if not changes:
        raise CalError("Nothing to set: name at least one value, e.g. --mid-offset -67.7")
    change_cal(args, changes, "To send:")


def cmd_restore(args):
    change_cal(args, load_cal(args.file), "To send (from %s):" % args.file)


def cmd_correct(args):
    if args.cal:
        cal, source = load_cal(args.cal), args.cal
    else:
        unit = open_unit(args.port)
        try:
            cal, _ = get_cal(unit)
        finally:
            unit.close()
        source = "CAL_GET on %s" % unit.name
    with open(args.log, "rb") as fh:
        data = fh.read()
    stale, packets = log_packets(data, args.t0, args.t1)
    window = Window(stale)
    with (open(args.csv, "x", newline="", encoding="utf-8") if args.csv
          else contextlib.nullcontext()) as out:
        writer = csv.writer(out) if out else None
        if writer:
            writer.writerow(["t_s", "k", "range", "vol_adc", "cur_adc", "ref_adc", "current_ua"])
        for t, samples in packets:
            window.add(t, samples)
            if writer:
                for k, sample in enumerate(samples):
                    writer.writerow(["%.6f" % t, k] + list(sample)
                                    + ["%.4f" % current_ua(sample, cal)])
    show_cal(cal, "Calibration (%s):" % source)
    for problem in problems(cal):
        print("  the firmware's validity check would refuse this: %s" % problem)
    if window.stale is not None:
        print("Dropped a stale first packet at %.3f s" % window.stale)
    if not window.packets:
        raise CalError("%s: no measurement packets in the window" % args.log)
    print("%d packets, device time %.3f .. %.3f s, %.3f s covered; VBUS %.4f V"
          % (window.packets, window.first, window.last, window.span(), window.vbus()))
    print("Gaps over %.1f ms: %d, %.1f ms with no packets (counted at the mean current)"
          % (GAP_S * 1e3, window.gaps, window.missing * 1e3))
    print("%d samples with ref_adc outside client 0.1.3's filter (%d-%d), kept as the "
          "firmware keeps them" % (window.outside_ref, CLIENT_REF[0], CLIENT_REF[1]))
    for r, name in RANGES.items():
        if window.count[r]:
            print("  %-4s %8d samples  mean %.3f uA calibrated, %.3f uA with gain 1, offset 0"
                  % (name, window.count[r], window.mean_ua(cal, (r,)),
                     window.mean_ua(DEFAULTS, (r,))))
    for label, c in (("Calibrated", cal), ("Gain 1, offset 0", DEFAULTS)):
        q = window.charge_uc(c)
        print("%-17s mean %.3f uA, charge %.3f uC (%.6f uAh)" % (label, window.mean_ua(c), q,
                                                                  q / 3600.0))
    if args.csv:
        print("Each sample's current written to %s" % args.csv)


COMMANDS = {"read": cmd_read, "measure": cmd_measure, "range": cmd_range, "set": cmd_set,
            "restore": cmd_restore, "correct": cmd_correct}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    port_help = "the serial port, e.g. COM3 (default: the only port with the PowerPico's USB IDs)"
    read = sub.add_parser("read", help="CAL_GET and RANGE_GET; writes nothing")
    read.add_argument("--save", help="also write what was read to this new JSON file")
    measure = sub.add_parser("measure", help="average the live stream; writes nothing")
    measure.add_argument("--seconds", type=float, default=5.0)
    rng = sub.add_parser("range", help="RANGE_SET, kept in RAM until a power cycle")
    rng.add_argument("mode", choices=list(MODES))
    change = sub.add_parser("set", help="CAL_SET with the values named, saved in the EEPROM")
    for k in FIELDS:
        change.add_argument("--" + k.replace("_ua", "").replace("_", "-"), type=float, dest=k,
                            metavar="UA" if k.endswith("_ua") else "G")
    restore = sub.add_parser("restore", help="CAL_SET from a file read --save wrote")
    restore.add_argument("file")
    for p in (change, restore):
        p.add_argument("--yes", action="store_true", help="send it; without this, only print it")
    correct = sub.add_parser("correct", help="apply a calibration to a pico_vlog.py log")
    correct.add_argument("log", help="a .bin file pico_vlog.py or pico_dlog.py wrote")
    source = correct.add_mutually_exclusive_group(required=True)
    source.add_argument("--cal", metavar="FILE", help="a JSON file read --save wrote")
    source.add_argument("--unit", action="store_true", help="read it from the unit by CAL_GET")
    correct.add_argument("--from", dest="t0", type=float, metavar="S",
                         help="start, in device seconds")
    correct.add_argument("--to", dest="t1", type=float, metavar="S", help="end, in device seconds")
    correct.add_argument("--csv", metavar="OUT",
                         help="write each sample's current to this new CSV file")
    for p in (read, measure, rng, change, restore, correct):
        p.add_argument("--port", help=port_help)
    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.cmd](args) or 0
    except (CalError, OSError) as error:
        print(error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
