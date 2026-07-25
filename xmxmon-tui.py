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

def post(path, obj):
    """Fire-and-forget POST to the daemon (capture/group controls)."""
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass


def menu_screen(snap, menu, width):
    """Full-screen picker for the [g] group switch.

    Two stages: choose a device (skipped when only one), then choose the group
    for it. The device's current group is marked. Selection is by number.
    """
    devs = sorted(snap.items())
    lines = ["\x1b[H\x1b[2Jxmxmon — switch metric group      "
             "[1-9] select   [Esc] cancel", ""]
    if menu["stage"] == "dev":
        lines.append(" choose device:")
        for i, (dev, s) in enumerate(devs, 1):
            lines.append(f"   {i})  device {dev}    (now: {s.get('group', '?')})")
    else:
        dev = menu["dev"]
        s = snap.get(dev, {})
        sw = s.get("switchable") or []
        cur = s.get("group")
        lines.append(f" device {dev} — choose group:")
        for i, g in enumerate(sw, 1):
            lines.append(f"   {i}) {'*' if g == cur else ' '} {g}")
        if s.get("capture"):
            lines.append("")
            lines.append(" note: this device is CAPTURING — switch will be "
                         "refused until you stop it")
    lines.append("")
    lines.append(" the switch is device-wide (every viewer sees it) and "
                 "reverts to config on restart")
    return lines


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
    elif unit == "B/s":
        g = value / 1e9
        s = f"{g:.1f}G" if g >= 0.1 else f"{value / 1e6:.0f}M"
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
    # ComputeBasic / MemoryProfile / DeviceCacheProfile
    "send / issued": "send/iss", "dispatch overhead": "dispatch",
    "compute engine busy": "cmd busy", "L1 hit rate": "L1 hit",
    "L3 hit rate": "L3 hit", "VRAM read": "VRAM rd", "VRAM write": "VRAM wr",
    "PCIe host→GPU": "PCIe→gpu", "GPU→sysmem": "gpu→sys",
    "SLM traffic": "SLM", "kernel dispatches": "kernel disp",
    "read coalescing": "rd coalesc", "write coalescing": "wr coalesc",
    "L3 superq full": "L3 superq", "mem queue full": "mem q full",
    "copy engine stall": "copy stall", "L1 partial writes": "L1 partial",
    "SLM bank conflicts": "SLM bankcf",
    "L3 from load/store": "L3 ld/st", "L3 from instruction": "L3 icache",
    "L3 from sampler": "L3 sampler", "load/store L3 hit": "ld/st L3hit",
    "L3 busy": "L3 busy", "L3→VRAM read": "L3→vram r",
    "L3→VRAM write": "L3→vram w",
}


def hero_lines(dev, s, peaks, barw):
    """Left-column bars/rates from the active group's hero spec (xmxderive).

    Each spec item renders itself; peaks are held per (device, metric) so bars
    keep a high-water mark across the session, matching the WUI's autoscale.
    """
    g, r = s.get("gauges", {}), s.get("rates", {})
    lines = []
    for item in xmxderive.hero(s.get("group")):
        kind, label = item[0], item[1]
        if kind == "pct":
            v = g.get(item[2], 0.0)
            pk = peaks[dev, item[2]] = max(peaks.get((dev, item[2]), 0), v)
            lines.append(f" {label:9s}[{bar(v, barw, pk)}]{v:5.1f}%")
        elif kind == "rate":
            v = r.get(item[2], 0.0)
            pk = peaks[dev, item[2]] = max(peaks.get((dev, item[2]), 0), v) or 1
            mark = "" if v > 0 else " (idle)"
            lines.append(f" {label:9s}[{bar(v / pk * 100, barw)}]{si(v)}/s{mark}")
        elif kind == "xmxgroup":
            keys = [k for _, k in item[2]]
            for k in keys:
                peaks[dev, k] = max(peaks.get((dev, k), 0), r.get(k, 0.0))
            gmax = max((peaks.get((dev, k), 0) for k in keys), default=1) or 1
            for sub, k in item[2]:
                v = r.get(k, 0.0)
                mark = "" if v > 0 else " (idle)"
                lines.append(f" {label} {sub:5s}[{bar(v / gmax * 100, barw)}]"
                             f"{si(v)}/s{mark}")
        elif kind in ("rwgb", "rwtx64"):
            scale = (64 if kind == "rwtx64" else 1) / 1e9
            rd = r.get(item[2][0], 0) * scale
            wr = r.get(item[2][1], 0) * scale
            lines.append(f" {label} R{rd:7.1f} W{wr:6.1f} GB/s")
        elif kind == "freq":
            lines.append(f" {label} {g.get(item[2], 0):.0f} MHz")
    return lines


def device_columns(dev, s, peaks, detailed, barw, right_w=42):
    """Return (left_lines, right_lines) for one device.

    The detail column packs into as many sub-columns as `right_w` allows, so a
    long metric list doesn't stretch the block far past the left column and
    push the next device off a standard 24-line terminal.
    """
    left = hero_lines(dev, s, peaks, barw)
    right = []

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
    for group, items in xmxderive.raw_rows(merged, s.get("group")):
        for k, val in items:
            short = (k.replace("XVE_INST_EXECUTED_", "")
                      .replace("COMMAND_PARSER_COMPUTE_ENGINE_DISPATCH_KERNEL_COUNT",
                               "KERNELS")
                      .replace("GPGPU_THREADGROUP_COUNT", "THREADGROUPS")
                      .replace("HOST_TO_GPUMEM_TRANSACTION_", "PCIe_")
                      .replace("SYSMEM_TRANSACTION_", "SYS_")
                      .replace("GPU_MEMORY_32B_TRANSACTION_", "32B_")
                      .replace("GPU_MEMORY_64B_TRANSACTION_", "64B_")
                      .replace("LOAD_STORE_CACHE_", "L1_")
                      .replace("GPU_MEMORY_BYTE_", "MEM_")
                      .replace("GPU_MEMORY_", "GTI_"))[:14]
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
    menu = None            # None, or {"stage": "dev"} / {"stage": "grp", "dev": d}
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
                   f"   {hint} [g] group [q] quit"]
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
            if menu is not None:                       # picker overlays the view
                out = menu_screen(snap, menu, width)
            print("\n".join(out), flush=True)
            if not interactive:
                time.sleep(0.5)
                continue
            t0 = time.time()
            while time.time() - t0 < 0.5:
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    ch = sys.stdin.read(1)
                    if menu is not None:               # menu captures all keys
                        if ch in ("\x1b", "q"):
                            menu = None
                        elif ch.isdigit():
                            devs = sorted(snap.items())
                            n = int(ch)
                            if menu["stage"] == "dev":
                                if 1 <= n <= len(devs):
                                    menu = {"stage": "grp", "dev": devs[n - 1][0]}
                            else:
                                sw = snap.get(menu["dev"], {}).get("switchable") or []
                                if 1 <= n <= len(sw):
                                    post("/group", {"device": int(menu["dev"]),
                                                    "group": sw[n - 1]})
                                    menu = None
                        break
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
                    if ch == "g":                      # open the picker
                        devs = sorted(snap.items())
                        menu = ({"stage": "grp", "dev": devs[0][0]}
                                if len(devs) == 1 else {"stage": "dev"})
                        break
    except KeyboardInterrupt:
        pass
    finally:
        if interactive:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print()

if __name__ == "__main__":
    main()
