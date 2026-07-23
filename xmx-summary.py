#!/usr/bin/env python3
# Copyright 2026 the xmxmon authors
# SPDX-License-Identifier: Apache-2.0
"""Summarize an xmxmon CSV: XMX verdict first, then key utilization metrics."""
import csv
import sys

XMX = ["XVE_INST_EXECUTED_XMX_INT2", "XVE_INST_EXECUTED_XMX_INT4",
       "XVE_INST_EXECUTED_XMX_INT8", "XVE_INST_EXECUTED_XMX_FP16",
       "XVE_INST_EXECUTED_XMX_BF16"]
KEY = ["GPU_BUSY", "XVE_ACTIVE", "XVE_STALL", "XVE_THREADS_OCCUPANCY_ALL",
       "XVE_INST_EXECUTED_ALU0_ALL", "XVE_INST_EXECUTED_ALU1_ALL",
       "XVE_INST_EXECUTED_ALU2_ALL", "XVE_INST_EXECUTED_SEND_ALL",
       "GPU_MEMORY_BYTE_READ", "GPU_MEMORY_BYTE_WRITE",
       "L3_HIT", "L3_MISS", "AvgGpuCoreFrequencyMHz"]

def main(path):
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
    return 0

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: xmx-summary.py capture.csv"); sys.exit(1)
    sys.exit(main(sys.argv[1]))
