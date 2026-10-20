#!/usr/bin/env python3
"""Analyse input VBUS in a stream file written by pico_vlog.py or pico_dlog.py.

Usage:
  py tools/pico_vtrace.py FILE.bin [--from S] [--to S] [--svg OUT.svg]

Prints, with times in device seconds (the packets' timestamps):

- the stream gaps: jumps of more than 1.5 ms between packets;
- the spans where VBUS is under 1 V (a source hard reset, or the output off)
  and over 4.75 V;
- the plateaus, VBUS steady for 100 ms or more, and the moves between them.

Each packet's ten samples are one row (its mean, min and max). A plateau is
a run of 20 ms blocks whose means stay within 30 mV of the plateau's running
mean, the row means within each block spanning less than 100 mV. --from and
--to keep the rows in that window of device time; the gaps are reported over
the whole file. With --svg it also draws VBUS over the window as a 10 ms
min/max envelope.

A first packet more than 1 s older than the second is dropped and reported:
it is not part of the run that follows.
"""

import argparse
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I leaves it out
import pico_stream  # noqa: E402

GAP = 0.0015
LOW, HIGH = 1.0, 4.75


def read_rows(data, t0=None, t1=None):
    """(rows, gaps, stale) from a stream file's bytes.

    rows: (t, mean, min, max) in seconds and volts, one per packet in the
    window; gaps: (t_before, t_after) pairs over the whole file; stale: the
    time of a dropped stale first packet, or None.
    """
    packets = ((item[1] / 1e6, item[2]) for item in pico_stream.parse(data) if item[0] == "adc")
    head = list(itertools.islice(packets, 2))
    stale = None
    if len(head) == 2 and head[1][0] - head[0][0] > 1.0:
        stale = head.pop(0)[0]
    rows, gaps = [], []
    last = None
    for t, volts in itertools.chain(head, packets):
        if last is not None and t - last > GAP:
            gaps.append((last, t))
        last = t
        if volts and (t0 is None or t >= t0) and (t1 is None or t <= t1):
            rows.append((t, sum(volts) / len(volts), min(volts), max(volts)))
    return rows, gaps, stale


def spans(rows, test, min_len=0.0):
    """[start, end, min, max] of each run of rows where test(min, max) holds."""
    out = []
    cur = None
    for t, _, vmin, vmax in rows:
        if test(vmin, vmax):
            if cur is None:
                cur = [t, t, vmin, vmax]
            cur[1] = t
            cur[2] = min(cur[2], vmin)
            cur[3] = max(cur[3], vmax)
        elif cur is not None:
            if cur[1] - cur[0] >= min_len:
                out.append(cur)
            cur = None
    if cur is not None and cur[1] - cur[0] >= min_len:
        out.append(cur)
    return out


def plateaus(rows, block=0.020, tol=0.030, min_len=0.100, steady=0.100):
    """Dicts with start, end, mean, lo and hi (the lowest and highest row
    mean) for each plateau of at least min_len seconds."""
    blocks = []
    cur = []
    for r in rows:
        if cur and r[0] - cur[0][0] >= block:
            blocks.append(cur)
            cur = []
        cur.append(r)
    if cur:
        blocks.append(cur)
    out = []
    p = None
    for b in blocks:
        means = [r[1] for r in b]
        mean = sum(means) / len(means)
        lo, hi = min(means), max(means)
        is_steady = hi - lo < steady
        if is_steady and p is not None and abs(mean - p["sum"] / p["n"]) < tol:
            p["end"] = b[-1][0]
            p["sum"] += mean
            p["n"] += 1
            p["lo"] = min(p["lo"], lo)
            p["hi"] = max(p["hi"], hi)
            continue
        if p is not None and p["end"] - p["start"] >= min_len:
            out.append(p)
        p = ({"start": b[0][0], "end": b[-1][0], "sum": mean, "n": 1, "lo": lo, "hi": hi}
             if is_steady else None)
    if p is not None and p["end"] - p["start"] >= min_len:
        out.append(p)
    for p in out:
        p["mean"] = p.pop("sum") / p.pop("n")
    return out


def svg(rows, path):
    """VBUS over the rows' time as a 10 ms min (blue) / max (red) envelope."""
    w, h = 1600, 500
    vlo, vhi = 0.0, 5.6
    t0, t1 = rows[0][0], rows[-1][0]
    env = {}
    for t, _, vmin, vmax in rows:
        k = int((t - t0) * 100)
        e = env.get(k)
        if e is None:
            env[k] = [vmin, vmax]
        else:
            e[0] = min(e[0], vmin)
            e[1] = max(e[1], vmax)
    span = max(t1 - t0, 1e-3)

    def x(t):
        return 60 + (t - t0) / span * (w - 80)

    def y(v):
        return 20 + (vhi - min(max(v, vlo), vhi)) / (vhi - vlo) * (h - 60)

    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d">' % (w, h),
             '<rect width="100%" height="100%" fill="white"/>']
    for v in (0, 1, 2, 3, 3.3, 3.7, 4, 4.2, 5):
        parts.append('<line x1="60" x2="%d" y1="%.1f" y2="%.1f" stroke="#ddd"/>' % (w - 20, y(v), y(v)))
        parts.append('<text x="5" y="%.1f" font-size="12">%.1f V</text>' % (y(v) + 4, v))
    step = 300
    for s in (1, 2, 5, 10, 20, 30, 60, 120, 300):
        if span / s <= 20:
            step = s
            break
    tt = (int(t0 / step) + 1) * step
    while tt < t1:
        parts.append('<line y1="20" y2="%d" x1="%.1f" x2="%.1f" stroke="#eee"/>' % (h - 40, x(tt), x(tt)))
        parts.append('<text y="%d" x="%.1f" font-size="12">%g</text>' % (h - 20, x(tt) - 10, tt))
        tt += step
    hi_points, lo_points = [], []
    for k in sorted(env):
        t = t0 + k / 100.0
        hi_points.append("%.1f,%.1f" % (x(t), y(env[k][1])))
        lo_points.append("%.1f,%.1f" % (x(t), y(env[k][0])))
    parts.append('<polyline fill="none" stroke="#c33" stroke-width="1" points="%s"/>'
                 % " ".join(hi_points))
    parts.append('<polyline fill="none" stroke="#33c" stroke-width="1" points="%s"/>'
                 % " ".join(lo_points))
    parts.append('</svg>')
    with open(path, "w") as handle:
        handle.write("\n".join(parts))


def report(rows, gaps, stale, say=print):
    if stale is not None:
        say("dropped a stale first packet at %.3f s" % stale)
    if not rows:
        say("no packets")
        return
    say("packets %d, device time %.3f .. %.3f s" % (len(rows), rows[0][0], rows[-1][0]))
    say("stream gaps over %.1f ms: %d" % (GAP * 1e3, len(gaps)))
    for g0, g1 in gaps[:20]:
        say("  gap %.3f -> %.3f (%.1f ms)" % (g0, g1, (g1 - g0) * 1e3))
    if len(gaps) > 20:
        say("  ... and %d more" % (len(gaps) - 20))
    say("VBUS under %g V:" % LOW)
    for s in spans(rows, lambda lo, hi: (lo + hi) / 2 < LOW):
        say("  %.3f .. %.3f (%.0f ms) min %.3f" % (s[0], s[1], (s[1] - s[0]) * 1e3, s[2]))
    say("VBUS over %g V:" % HIGH)
    for s in spans(rows, lambda lo, hi: (lo + hi) / 2 > HIGH):
        say("  %.3f .. %.3f (%.0f ms) max %.3f" % (s[0], s[1], (s[1] - s[0]) * 1e3, s[3]))
    say("plateaus (start, length, mean, lowest..highest 1 ms mean) and the moves between them:")
    prev = None
    for p in plateaus(rows):
        if prev is not None:
            seg = [r for r in rows if prev["end"] < r[0] < p["start"]]
            lo = min((r[2] for r in seg), default=float("nan"))
            hi = max((r[3] for r in seg), default=float("nan"))
            say("      move %.0f ms, min %.3f max %.3f" % ((p["start"] - prev["end"]) * 1e3, lo, hi))
        say("  %9.3f  %7.3f s  %.3f V  %.3f..%.3f" % (
            p["start"], p["end"] - p["start"], p["mean"], p["lo"], p["hi"]))
        prev = p


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("file")
    parser.add_argument("--from", dest="t0", type=float, help="start, in device seconds")
    parser.add_argument("--to", dest="t1", type=float, help="end, in device seconds")
    parser.add_argument("--svg", help="also draw VBUS into this SVG file")
    args = parser.parse_args(argv)
    with open(args.file, "rb") as handle:
        data = handle.read()
    rows, gaps, stale = read_rows(data, args.t0, args.t1)
    report(rows, gaps, stale)
    if not rows:
        return 1
    if args.svg:
        svg(rows, args.svg)
        print("svg written to", args.svg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
