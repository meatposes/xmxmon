#!/usr/bin/env python3
# Copyright 2026 the xmxmon authors
# SPDX-License-Identifier: Apache-2.0
"""xmxmon-tui — terminal live view; thin client on a running xmxmond.

usage: xmxmon-tui.py [--detailed] [http://host:9143]
Works over ssh (pure ANSI, 2 Hz refresh). d toggles overhead detail, q quits.
"""
import json
import select
import shutil
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
    """Format a derived value into exactly 8 columns, so detail cells align."""
    if value is None:
        s = "—"
    elif unit == "%":
        s = f"{value:.1f}%"
    elif unit == "x":
        s = f"{value:.2f}x"
    elif unit == "/s":
        s = f"{si(value).strip()}/s"
    elif abs(value) < 100:
        s = f"{value:.3f}"
    else:
        s = si(value).strip()
    return f"{s:>8s}"[:8]


# Compact labels for the narrow detail column. Falls back to the full label
# (truncated) for anything not listed, so new derived metrics still render.
SHORT = {
    "prep work / XMX": "prep/XMX", "XMX per VRAM byte": "XMX per byte",
    "L3 hit rate": "L3 hit", "L3 stall": "L3 stall",
    "mem queue full": "mem q full", "kernel dispatches": "kernel disp",
    "barrier share": "barriers", "memory ops / XMX": "memop/XMX",
    "divergent issue": "divergent", "icache miss": "icache miss",
    "multi-pipe active": "multi-pipe",
}


def device_columns(dev, s, peaks, detailed, barw, right_w=42):
    """Return (left_lines, right_lines) for one device.

    The detail column packs into as many sub-columns as `right_w` allows, so a
    long metric list doesn't stretch the block far past the left column and
    push the next device off a standard 24-line terminal.
    """
    g, r = s.get("gauges", {}), s.get("rates", {})
    left, right = [], []

    for label, key in (("busy", "GPU_BUSY"), ("XVE act", "XVE_ACTIVE"),
                       ("occupancy", "XVE_THREADS_OCCUPANCY_ALL")):
        v = g.get(key, 0.0)
        pk = peaks[dev, key] = max(peaks.get((dev, key), 0), v)
        left.append(f" {label:9s}[{bar(v, barw, pk)}]{v:5.1f}%")

    xmx_max = max((peaks.get((dev, k), 0) for _, k in XMX), default=1) or 1
    idle = []
    for label, key in XMX:
        v = r.get(key, 0.0)
        pk = peaks[dev, key] = max(peaks.get((dev, key), 0), v)
        if v <= 0 and pk <= 0:
            idle.append(label)          # collapse never-used paths to one line
            continue
        left.append(f" XMX {label:5s}[{bar(v / xmx_max * 100, barw)}]"
                    f"{si(v)}/s")
    if idle:
        left.append(f" XMX idle: {' '.join(idle)}")

    rd = r.get("GPU_MEMORY_BYTE_READ", 0) / 1e9
    wr = r.get("GPU_MEMORY_BYTE_WRITE", 0) / 1e9
    left.append(f" mem R{rd:7.1f} W{wr:6.1f} GB/s")
    left.append(f" freq {g.get('AvgGpuCoreFrequencyMHz', 0):.0f} MHz")

    if detailed:
        cells = [f" {SHORT.get(d['label'], d['label'])[:12]:12s}"
                 f"{fmt(d['value'], d['unit'])}"
                 for d in s.get("derived") or []]
        right.append("OVERHEAD")
        if not cells:
            right.append(" (none for this metric group)")
        else:
            cw = 21
            ncol = max(1, right_w // cw)
            for i in range(0, len(cells), ncol):
                right.append("".join(f"{c:<{cw}}"
                                     for c in cells[i:i + ncol]).rstrip())
    return left, right


def raw_block(s, width):
    """Compact multi-column raw counter grid spanning the full width."""
    merged = dict(s.get("rates", {}))
    merged.update(s.get("gauges", {}))
    cells = []
    for group, items in xmxderive.raw_rows(merged):
        for k, val in items:
            short = (k.replace("XVE_INST_EXECUTED_", "")
                      .replace("COMMAND_PARSER_COMPUTE_ENGINE_DISPATCH_KERNEL_COUNT",
                               "KERNELS")
                      .replace("GPGPU_THREADGROUP_COUNT", "THREADGROUPS")
                      .replace("GPU_MEMORY_BYTE_", "MEM_"))[:14]
            cells.append(f" {short:14s}{si(val).strip():>8s}/s")
    if not cells:
        return []
    cw = 26
    ncol = max(1, width // cw)
    head = "  ── raw counters (per second) "
    lines = [(head + "─" * max(0, width - len(head)))[:width]]
    for i in range(0, len(cells), ncol):
        lines.append("".join(f"{c:<{cw}}" for c in cells[i:i + ncol]).rstrip())
    return lines


def main():
    detailed = DETAILED
    show_raw = False
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
            width = shutil.get_terminal_size((80, 24)).columns
            # Two columns need ~72 cols; below that stack them instead.
            two_col = detailed and width >= 72
            lw = 38 if two_col else 0
            barw = 10 if detailed else 22

            hint = "[d] detail" if not detailed else \
                   ("[d] off [r] raw" if not show_raw else "[d] off [r] hide raw")
            out = [f"\x1b[H\x1b[2Jxmxmon — {time.strftime('%H:%M:%S')}"
                   f"   {hint} [q] quit"]
            for dev, s in sorted(snap.items()):
                cap = s.get("capture")
                state = (f"CAPTURING {cap['name']} ({cap['rows']}r)" if cap
                         else "idle")
                head = (f"── dev {dev}  {s.get('group','?')}  "
                        f"{s.get('period_ms','?')}ms  {state} ")
                out.append((head + "─" * max(0, width - len(head)))[:width])
                left, right = device_columns(
                    dev, s, peaks, detailed, barw,
                    max(21, width - lw) if two_col else max(21, width))
                if two_col:
                    for i in range(max(len(left), len(right))):
                        l = left[i] if i < len(left) else ""
                        r_ = right[i] if i < len(right) else ""
                        out.append(f"{l:<{lw}}{r_}" if r_ else l)
                else:
                    out.extend(left)
                    out.extend(right)
                if detailed and show_raw:
                    out.extend(raw_block(s, width))
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
                        if not detailed:
                            show_raw = False
                        break
                    if ch == "r" and detailed:
                        show_raw = not show_raw
                        break
    except KeyboardInterrupt:
        pass
    finally:
        if interactive:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print()

if __name__ == "__main__":
    main()
