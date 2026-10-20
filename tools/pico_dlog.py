#!/usr/bin/env python3
"""Log the PowerPico's USB stream and print the debug build's events as they come.

Usage:
  py tools/pico_dlog.py OUT_PREFIX [--wait S] [--max S] [--quiet-fusb]

pico_vlog.py (the raw bytes to OUT_PREFIX-N.bin, VBUS once a second, STALL
when no packet arrives), with --max 3600 by default, and one line for each
frame the debug build (branch pps-debug-trace) sends: each event, as

  14:02:11    81234567 us  TX_REQUEST Request id2 [flagged] (pos 5, 3300 mV, 1000 mA)

the PC's time, the unit's TIM5 time and the decoded event; the boot summary;
and, after a crash, its record and the events before it. Command answers are
printed too. --quiet-fusb leaves out the FUSB302 interrupt events, the most
numerous. Any firmware's stream can be logged; the release firmware simply
sends no debug frames.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I leaves it out
import pico_serial  # noqa: E402
import pico_stream  # noqa: E402
import pico_vlog  # noqa: E402

EV_FUSB = 13  # DBG_EV_FUSB_STATUS


def printer(say, quiet_fusb=False):
    """An on_item for pico_vlog.log that prints debug frames and command frames."""
    def on_item(item):
        if item[0] == "dbg":
            ftype, payload = item[1], item[3]
            if ftype == pico_stream.DBG_EVENTS:
                for t, kind, a, b, c in pico_stream.events(payload):
                    if not (quiet_fusb and kind == EV_FUSB):
                        say("%s %11d us  %s" % (pico_vlog.clock(), t,
                                                pico_stream.describe(kind, a, b, c)))
            else:
                say("%s %s" % (pico_vlog.clock(), pico_stream.describe_frame(ftype, payload)))
        elif item[0] == "cmd":
            say("%s %s" % (pico_vlog.clock(), pico_stream.describe_command(*item[1:])))
    return on_item


def main(argv=None):
    parser = pico_vlog.arguments(__doc__.split("\n\n")[0], 3600)
    parser.add_argument("--quiet-fusb", action="store_true",
                        help="leave out the FUSB302 interrupt events")
    args = parser.parse_args(argv)
    if pico_serial.backend() is None:
        print(pico_serial.NEEDS_PYSERIAL)
        return 1

    def say(text):
        print(text, flush=True)

    try:
        return pico_vlog.log(args.prefix, args.wait, args.max,
                             on_item=printer(say, args.quiet_fusb), say=say)
    except KeyboardInterrupt:
        print("stopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
