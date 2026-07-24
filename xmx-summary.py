#!/usr/bin/env python3
# Copyright 2026 the xmxmon authors
# SPDX-License-Identifier: Apache-2.0
"""Summarize an xmxmon CSV: XMX verdict first, then key utilization metrics.

With --detailed, adds derived overhead ratios (operand-prep cost, arithmetic
intensity, cache and dispatch behaviour) and the raw counters behind them.
"""
import csv
import sys

import xmxderive

XMX = ["XVE_INST_EXECUTED_XMX_INT2", "XVE_INST_EXECUTED_XMX_INT4",
       "XVE_INST_EXECUTED_XMX_INT8", "XVE_INST_EXECUTED_XMX_FP16",
       "XVE_INST_EXECUTED_XMX_BF16"]
KEY = ["GPU_BUSY", "XVE_ACTIVE", "XVE_STALL", "XVE_THREADS_OCCUPANCY_ALL",
       "XVE_INST_EXECUTED_ALU0_ALL", "XVE_INST_EXECUTED_ALU1_ALL",
       "XVE_INST_EXECUTED_ALU2_ALL", "XVE_INST_EXECUTED_SEND_ALL",
       "GPU_MEMORY_BYTE_READ", "GPU_MEMORY_BYTE_WRITE",
       "L3_HIT", "L3_MISS", "AvgGpuCoreFrequencyMHz"]

def si(v):
    for t, s in ((1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= t:
            return f"{v/t:10.3f}{s}"
    return f"{v:10.3f} "


def fmt(value, unit):
    if value is None or value != value:
        return "         —"
    if value in (float("inf"), float("-inf")):
        return "       n/a"
    if unit == "%":
        return f"{value:10.1f}%"
    if unit == "x":
        return f"{value:10.3f}x"
    if unit == "/s":
        return si(value)
    return f"{value:10.4g} "


def detailed_section(busy, elapsed_s):
    """Derived overhead metrics over the busy portion of a capture."""
    cols = busy[0].keys()
    totals = {c: sum(float(r.get(c) or 0) for r in busy)
              for c in cols if c != "t_s"}
    raw_totals = dict(totals)
    # Normalize the same way the daemon does — percentages averaged, counters
    # converted to per-second — so derived values mean the same thing whether
    # they came from a live snapshot or an offline capture.
    for c in list(totals):
        if xmxderive.is_percent(c):
            totals[c] = totals[c] / len(busy)
        else:
            totals[c] = totals[c] / elapsed_s

    print("\n--- overhead (derived) ---")
    derived = xmxderive.derive(totals)
    if not derived:
        print("no derived metrics available for this metric group")
    for label, value, unit, note in derived:
        suffix = f"   {note}" if note else ""
        print(f"{label:26s} {fmt(value, unit)}{suffix}")

    print(f"\n--- raw counters (total over {elapsed_s:.1f}s busy; rate in parens) ---")
    for group, items in xmxderive.raw_rows(raw_totals):
        print(f"  {group}:")
        for k, val in items:
            print(f"    {k:52s} {si(val)}  ({si(val / elapsed_s)}/s)")


def main(path, detailed=False):
    rows = list(csv.DictReader(open(path)))
    if not rows:
        print("empty capture"); return 1
    cols = rows[0].keys()
    busy = [r for r in rows if float(r.get("GPU_BUSY", 0) or 0) > 1.0]
    print(f"{len(rows)} reports total, {len(busy)} with GPU_BUSY > 1% "
          f"(stats below are over busy reports only)")
    if not busy:
        print("no GPU activity captured — was the workload on this device?")
        return 1

    def stats(c):
        vs = [float(r[c] or 0) for r in busy]
        return sum(vs), sum(vs) / len(vs), max(vs)

    print("\n--- XMX (matrix engine) ---")
    any_xmx = False
    for c in XMX:
        if c not in cols:
            continue
        total, avg, peak = stats(c)
        mark = "  <-- ACTIVE" if total > 0 else ""
        if total > 0:
            any_xmx = True
        print(f"{c:36s} total {total:12.4g}  avg {avg:10.4g}  peak {peak:10.4g}{mark}")
    print("\nVERDICT: XMX " + ("IS being used (see precisions above)"
                               if any_xmx else "NOT used — pure vector/ALU workload"))

    print("\n--- utilization ---")
    for c in KEY:
        if c not in cols:
            continue
        total, avg, peak = stats(c)
        print(f"{c:36s} avg {avg:12.4g}  peak {peak:12.4g}")

    if detailed:
        ts = [float(r["t_s"]) for r in busy]
        detailed_section(busy, max(ts) - min(ts) or 1.0)
    else:
        print("\n(run with --detailed for operand-prep cost, cache and "
              "dispatch overhead)")
    return 0

if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    want_detail = "--detailed" in args
    paths = [a for a in args if not a.startswith("-")]
    if len(paths) != 1:
        print("usage: xmx-summary.py [--detailed] capture.csv"); sys.exit(1)
    sys.exit(main(paths[0], want_detail))
