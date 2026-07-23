#!/usr/bin/env python3
# Copyright 2026 the xmxmon authors
# SPDX-License-Identifier: Apache-2.0
"""xmxmon-tui — terminal live view; thin client on a running xmxmond.

usage: xmxmon-tui.py [--detailed] [http://host:9143]
Works over ssh (pure ANSI, 2 Hz refresh). d toggles overhead detail, q quits.
"""
import json
import select
import sys
import termios
import time
import tty
import urllib.request

import xmxderive

ARGS = [a for a in sys.argv[1:]]
DETAILED = "--detailed" in ARGS or "-d" in ARGS
POS = [a for a in ARGS if not a.startswith("-")]
BASE = POS[0] if POS else "http://localhost:9143"
XMX = [("INT2", "XVE_INST_EXECUTED_XMX_INT2"), ("INT4", "XVE_INST_EXECUTED_XMX_INT4"),
       ("INT8", "XVE_INST_EXECUTED_XMX_INT8"), ("FP16", "XVE_INST_EXECUTED_XMX_FP16"),
       ("BF16", "XVE_INST_EXECUTED_XMX_BF16")]

def si(v):
    for t, s in ((1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k")):
        if v >= t:
            return f"{v/t:6.1f}{s}"
    return f"{v:6.0f} "

def bar(pct, width=30, peak=None):
    n = max(0, min(width, int(pct / 100 * width)))
    b = "#" * n + "-" * (width - n)
    if peak is not None:
        p = max(0, min(width - 1, int(peak / 100 * width)))
        b = b[:p] + "|" + b[p + 1:]
    return b

def fmt(value, unit):
    if value is None:
        return "     —"
    if unit == "%":
        return f"{value:6.1f}%"
    if unit == "x":
        return f"{value:6.2f}x"
    if unit == "/s":
        return f"{si(value)}/s"
    if abs(value) < 100:
        return f"{value:6.3f} "
    return f"{si(value)} "


def main():
    detailed = DETAILED
    peaks = {}
    # Raw-mode key polling only works on a real terminal; when piped or run
    # under nohup, fall back to plain refreshes and let SIGINT do the quitting.
    interactive = sys.stdin.isatty()
    fd = sys.stdin.fileno() if interactive else -1
    old = termios.tcgetattr(fd) if interactive else None
    if interactive:
        tty.setcbreak(fd)
    print("\x1b[2J", end="")
    try:
        while True:
            try:
                snap = json.load(urllib.request.urlopen(BASE + "/now", timeout=3))
            except Exception as e:
                print(f"\x1b[H\x1b[2Jxmxmond unreachable at {BASE}: {e}")
                time.sleep(2)
                continue
            hint = "[d] hide detail  [q] quit" if detailed else "[d] detail  [q] quit"
            out = [f"\x1b[H\x1b[2Jxmxmon TUI — {BASE}   "
                   f"{time.strftime('%H:%M:%S')}   {hint}"]
            for dev, s in sorted(snap.items()):
                g, r = s.get("gauges", {}), s.get("rates", {})
                cap = s.get("capture")
                out.append("")
                out.append(f"== device {dev}  "
                           f"{'CAPTURING ' + cap['name'] + ' (' + str(cap['rows']) + ' rows)' if cap else 'idle sampling'}"
                           f"  {s.get('group','?')}  period {s.get('period_ms','?')}ms ==")
                for label, key, unit in (("GPU busy", "GPU_BUSY", "%"),
                                         ("XVE active", "XVE_ACTIVE", "%"),
                                         ("Occupancy", "XVE_THREADS_OCCUPANCY_ALL", "%")):
                    v = g.get(key, 0.0)
                    pk = peaks[dev, key] = max(peaks.get((dev, key), 0), v)
                    out.append(f"  {label:11s} [{bar(v, peak=pk)}] {v:5.1f}{unit}")
                xmx_max = max((peaks.get((dev, k), 0) for _, k in XMX), default=1) or 1
                for label, key in XMX:
                    v = r.get(key, 0.0)
                    pk = peaks[dev, key] = max(peaks.get((dev, key), 0), v)
                    pct = v / xmx_max * 100 if xmx_max else 0
                    mark = "" if v > 0 else "   (inactive)"
                    out.append(f"  XMX {label:7s} [{bar(pct)}] {si(v)}/s{mark}")
                rd, wr = r.get("GPU_MEMORY_BYTE_READ", 0) / 1e9, r.get("GPU_MEMORY_BYTE_WRITE", 0) / 1e9
                out.append(f"  mem R/W     {rd:6.1f} / {wr:5.1f} GB/s"
                           f"    freq {g.get('AvgGpuCoreFrequencyMHz', 0):4.0f} MHz")

                if not detailed:
                    continue
                derived = s.get("derived") or []
                if derived:
                    out.append("  ── overhead ──────────────────────────────")
                    for d in derived:
                        note = f"   {d['note']}" if d.get("note") else ""
                        out.append(f"  {d['label']:22s} "
                                   f"{fmt(d['value'], d['unit'])}{note}")
                merged = dict(r)
                merged.update(g)
                rows = xmxderive.raw_rows(merged)
                if rows:
                    out.append("  ── raw counters (per second) ─────────────")
                    for group, items in rows:
                        out.append(f"  {group}:")
                        for k, val in items:
                            short = k.replace("XVE_INST_EXECUTED_", "") \
                                     .replace("COMMAND_PARSER_COMPUTE_ENGINE_", "")
                            out.append(f"    {short:34s} {si(val)}/s")
            print("\n".join(out), flush=True)
            if not interactive:
                time.sleep(0.5)
                continue
            t0 = time.time()
            while time.time() - t0 < 0.5:
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    ch = sys.stdin.read(1)
                    if ch == "q":
                        return
                    if ch == "d":
                        detailed = not detailed
                        break
    except KeyboardInterrupt:
        pass
    finally:
        if interactive:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print()

if __name__ == "__main__":
    main()
