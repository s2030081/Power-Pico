#!/usr/bin/env python3
"""Log the PowerPico's USB stream to a file and print input VBUS once a second.

Usage:
  py tools/pico_vlog.py OUT_PREFIX [--wait S] [--max S]

Waits up to --wait seconds (default 1800) for the PowerPico's port, then
writes every byte the unit sends to OUT_PREFIX-1.bin. When the port goes away
(the unit unplugged or reset) it waits for it again, up to --wait seconds,
and carries on in OUT_PREFIX-2.bin, and so on, until --max seconds (default
1800) have been logged in all. Ctrl-C stops it;
the file keeps everything received. Once a second it prints

  14:02:11 dev=812.004 n=1000 V min/mean/max 3.287 3.301 3.318 gaps=0

the PC's time, the device's timestamp in seconds, the measurement packets in
that second, VBUS over their samples, and the jumps of more than 1.5 ms in
the device's timestamps since the port was opened; or STALL when no packet
came in that second. pico_vtrace.py analyses the .bin files afterwards;
pico_dlog.py is this logger with the debug build's events printed as well.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I leaves it out
import pico_serial  # noqa: E402
import pico_stream  # noqa: E402

GAP_US = 1500


def clock():
    return time.strftime("%H:%M:%S")


class Summary:
    """VBUS over the packets since the last line, and the timestamp gaps."""

    def __init__(self):
        self.last_ts = None
        self.gaps = 0
        self.reset()

    def reset(self):
        self.packets = 0
        self.samples = 0
        self.total = 0.0
        self.low = self.high = None

    def add(self, ts_us, volts):
        if self.last_ts is not None and ts_us - self.last_ts > GAP_US:
            self.gaps += 1
        self.last_ts = ts_us
        self.packets += 1
        if volts:
            self.samples += len(volts)
            self.total += sum(volts)
            low, high = min(volts), max(volts)
            self.low = low if self.low is None else min(self.low, low)
            self.high = high if self.high is None else max(self.high, high)

    def line(self):
        if not self.packets:
            return "%s STALL: no packet this second" % clock()
        mean = self.total / self.samples if self.samples else float("nan")
        return "%s dev=%.3f n=%d V min/mean/max %.3f %.3f %.3f gaps=%d" % (
            clock(), self.last_ts / 1e6, self.packets, self.low, mean, self.high, self.gaps)


def connect(wait, say, poll):
    """(name, port) of the PowerPico's port once it opens, or None after
    `wait` seconds."""
    deadline = time.monotonic() + wait
    while True:
        ports = pico_serial.list_ports()
        if len(ports) > 1:
            say("%d PowerPico ports (%s); using %s" % (
                len(ports), ", ".join(p.device for p in ports), ports[0].device))
        if ports:
            try:
                return ports[0].device, pico_serial.open_port(ports[0].device, timeout=0.2)
            except OSError:
                pass  # still enumerating, or held by another program
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def record(port, path, budget, on_item, say, interval):
    """Copy the port to `path` for up to `budget` seconds or until the port
    goes away. Returns the seconds spent."""
    stream = pico_stream.Stream()
    summary = Summary()
    started = last_line = time.monotonic()
    with open(path, "wb") as out:
        while time.monotonic() - started < budget:
            try:
                data = port.read(65536)
            except OSError as error:
                say("%s port lost (%s)" % (clock(), error))
                break
            if data:
                out.write(data)
                out.flush()
                for item in stream.feed(data):
                    if item[0] == "adc":
                        summary.add(item[1], item[2])
                    if on_item:
                        on_item(item)
            now = time.monotonic()
            if now - last_line >= interval:
                last_line = now
                say(summary.line())
                summary.reset()
    return time.monotonic() - started


def log(prefix, wait=1800.0, max_seconds=1800.0, on_item=None, say=print,
        poll=0.5, interval=1.0):
    """The logging loop of pico_vlog and pico_dlog. `on_item` sees every item
    pico_stream parses. Returns the exit status."""
    logged = 0.0
    segment = 0
    say("waiting for the PowerPico's port (USB %04X:%04X) ..."
        % (pico_serial.USB_VID, pico_serial.USB_PID))
    while logged < max_seconds:
        found = connect(wait, say, poll)
        if found is None:
            say("%s no port that opens within %.0f s; giving up" % (clock(), wait))
            return 1 if segment == 0 else 0
        name, port = found
        segment += 1
        path = "%s-%d.bin" % (prefix, segment)
        say("%s opened %s -> %s" % (clock(), name, path))
        try:
            logged += record(port, path, max_seconds - logged, on_item, say, interval)
        finally:
            try:
                port.close()
            except OSError:
                pass
    say("done after %.0f s logged" % logged)
    return 0


def arguments(description, max_default):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("prefix", help="write OUT_PREFIX-1.bin, OUT_PREFIX-2.bin, ...")
    parser.add_argument("--wait", type=float, default=1800,
                        help="seconds to wait for the port each time (default 1800)")
    parser.add_argument("--max", type=float, default=max_default,
                        help="seconds to log in all (default %d)" % max_default)
    return parser


def main(argv=None):
    args = arguments(__doc__.split("\n\n")[0], 1800).parse_args(argv)
    if pico_serial.backend() is None:
        print(pico_serial.NEEDS_PYSERIAL)
        return 1
    try:
        return log(args.prefix, args.wait, args.max, say=lambda text: print(text, flush=True))
    except KeyboardInterrupt:
        print("stopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
