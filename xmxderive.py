# Copyright 2026 the xmxmon authors
# SPDX-License-Identifier: Apache-2.0
"""Derived overhead metrics shared by the daemon, TUI, web UI, and summary.

Raw counters answer "what happened". These ratios answer "how much of it was
useful work" — the numbers you actually compare between backends.

Every ratio divides two counters of the same kind, so it reads identically
whether you pass per-second rates (live view) or totals over a capture
(offline analysis). Absolute quantities are labelled with their unit and are
only meaningful when the inputs are rates.

Metrics absent from the active group are skipped rather than treated as zero,
so this adapts to whichever metric group is being sampled.
"""

XMX = ["XVE_INST_EXECUTED_XMX_INT2", "XVE_INST_EXECUTED_XMX_INT4",
       "XVE_INST_EXECUTED_XMX_INT8", "XVE_INST_EXECUTED_XMX_FP16",
       "XVE_INST_EXECUTED_XMX_BF16"]

# Work done to prepare operands rather than to multiply them: unpacking and
# widening quantized weights, applying scales, converting types.
PREP = ["XVE_INST_EXECUTED_BITCONV", "XVE_INST_EXECUTED_INT16",
        "XVE_INST_EXECUTED_INT32", "XVE_INST_EXECUTED_FP16",
        "XVE_INST_EXECUTED_FP32", "XVE_INST_EXECUTED_MATH"]

# Measured on a dense fp16 matmul (PyTorch XPU, 4096^3) — a workload that does
# essentially no operand preparation. Use it as the floor to compare against,
# not as a hardware specification.
PREP_REFERENCE = 0.034

# Metrics that are already levels (percentages, frequencies) and must be
# AVERAGED over a window, never summed as per-second counters. The daemon and
# the offline summary both consume this so their aggregation agrees. Any
# metric name ending in a stall/active percentage also matches by prefix in
# the consumers; this set covers the rest.
PERCENT = {
    "GPU_BUSY", "XVE_ACTIVE", "XVE_STALL", "XVE_THREADS_OCCUPANCY_ALL",
    "XVE_MULTIPLE_PIPE_ACTIVE", "XVE_PIPE_ALU0_AND_ALU1_ACTIVE",
    "XVE_PIPE_ALU0_AND_ALU2_ACTIVE",
    "L3_BUSY", "L3_STALL", "L3_INPUT_AVAILABLE", "L3_OUTPUT_READY",
    "L3_SUPERQ_FULL", "GPU_MEMORY_REQUEST_QUEUE_FULL",
    "COMMAND_PARSER_COMPUTE_ENGINE_BUSY", "COMMAND_PARSER_RENDER_ENGINE_BUSY",
    "AvgGpuCoreFrequencyMHz", "CoreFrequencyMHz", "ResultUncertainty",
    "XVE_INST_EXECUTED_ALU0_ALL_UTILIZATION",
    "XVE_INST_EXECUTED_ALU1_ALL_UTILIZATION",
    "XVE_INST_EXECUTED_ALU2_ALL_UTILIZATION",
}


def is_percent(metric):
    """True if a metric is a level (averaged), not a per-second counter."""
    return metric in PERCENT or metric.startswith("XVE_STALL_") \
        or metric.endswith("_UTILIZATION")


def _has(v, keys):
    return any(k in v for k in keys)


def _sum(v, keys):
    return sum(float(v.get(k, 0) or 0) for k in keys)


def _get(v, k):
    return float(v.get(k, 0) or 0)


def derive(v):
    """Compute derived metrics from a dict of counter values.

    Returns an ordered list of (label, value, unit, note) tuples. `unit` is
    "x" for ratios, "%" for percentages, "/s" for rates, "" for plain counts.
    `note` is a short interpretation hint or None.
    """
    out = []
    xmx = _sum(v, XMX)
    prep = _sum(v, PREP)

    if _has(v, XMX) and _has(v, PREP):
        if xmx > 0:
            out.append(("prep work / XMX", prep / xmx, "x",
                        f"ref {PREP_REFERENCE:.2f} = dense fp16 matmul"))
        elif prep > 0:
            out.append(("prep work / XMX", float("inf"), "x",
                        "no XMX work at all — vector path"))

    if "GPU_MEMORY_BYTE_READ" in v and _has(v, XMX):
        rd = _get(v, "GPU_MEMORY_BYTE_READ")
        if rd > 0:
            out.append(("XMX per VRAM byte", xmx / rd, "",
                        "arithmetic intensity on the matrix engine"))

    hit, miss = _get(v, "L3_HIT"), _get(v, "L3_MISS")
    if "L3_HIT" in v and hit + miss > 0:
        out.append(("L3 hit rate", 100.0 * hit / (hit + miss), "%",
                    "low = operands spilling to VRAM"))
    if "L3_STALL" in v:
        out.append(("L3 stall", _get(v, "L3_STALL"), "%", None))
    if "GPU_MEMORY_REQUEST_QUEUE_FULL" in v:
        out.append(("mem queue full", _get(v, "GPU_MEMORY_REQUEST_QUEUE_FULL"),
                    "%", "memory system saturation"))

    if "COMMAND_PARSER_COMPUTE_ENGINE_DISPATCH_KERNEL_COUNT" in v:
        out.append(("kernel dispatches",
                    _get(v, "COMMAND_PARSER_COMPUTE_ENGINE_DISPATCH_KERNEL_COUNT"),
                    "/s", "launch overhead"))

    # Barrier and control counters are thread-wide execution slots, so they
    # only mean anything against total issued instructions — not per kernel.
    issued = _get(v, "XVE_INST_ISSUED_ALL")
    if "XVE_INST_EXECUTED_BARRIER" in v and issued > 0:
        out.append(("barrier share",
                    100.0 * _get(v, "XVE_INST_EXECUTED_BARRIER") / issued, "%",
                    "sync cost as share of issued work"))

    if _has(v, XMX) and "XVE_INST_EXECUTED_SEND_ALL" in v and xmx > 0:
        out.append(("memory ops / XMX",
                    _get(v, "XVE_INST_EXECUTED_SEND_ALL") / xmx, "x",
                    "load/store pressure per unit of matrix work"))

    if "XVE_INST_EXECUTED_NONDIVERGENT" in v and issued > 0:
        nd = _get(v, "XVE_INST_EXECUTED_NONDIVERGENT")
        out.append(("divergent issue", max(0.0, 100.0 * (1 - nd / issued)), "%",
                    "branch divergence"))

    ih, im = _get(v, "ICACHE_HIT"), _get(v, "ICACHE_MISS")
    if "ICACHE_MISS" in v and ih + im > 0:
        out.append(("icache miss", 100.0 * im / (ih + im), "%",
                    "high = oversized or spilling kernels"))

    if "XVE_MULTIPLE_PIPE_ACTIVE" in v:
        out.append(("multi-pipe active", _get(v, "XVE_MULTIPLE_PIPE_ACTIVE"),
                    "%", "instruction-level parallelism"))

    # Stall breakdown, present only when sampling the VectorEngineStalls group.
    stalls = [(k, _get(v, k)) for k in sorted(v) if k.startswith("XVE_STALL_")]
    for k, val in sorted(stalls, key=lambda kv: -kv[1]):
        out.append((k.replace("XVE_STALL_", "stall: ").lower(), val, "%", None))

    return out


# Raw counters worth showing beneath the ratios, grouped for display.
RAW_GROUPS = [
    ("operand prep", PREP),
    ("matrix engine", XMX),
    ("vector pipes", ["XVE_INST_EXECUTED_ALU0_ALL", "XVE_INST_EXECUTED_ALU1_ALL",
                      "XVE_INST_EXECUTED_ALU2_ALL", "XVE_INST_EXECUTED_SEND_ALL",
                      "XVE_INST_ISSUED_ALL"]),
    ("memory", ["GPU_MEMORY_BYTE_READ", "GPU_MEMORY_BYTE_WRITE", "TLB_MISS"]),
    ("cache", ["L3_HIT", "L3_MISS", "L3_READ", "L3_WRITE",
               "ICACHE_HIT", "ICACHE_MISS"]),
    ("dispatch", ["COMMAND_PARSER_COMPUTE_ENGINE_DISPATCH_KERNEL_COUNT",
                  "GPGPU_THREADGROUP_COUNT", "XVE_INST_EXECUTED_BARRIER",
                  "XVE_INST_EXECUTED_CONTROL_ALL"]),
]


def raw_rows(v):
    """Ordered (group, metric, value) for the raw section; skips absent keys."""
    rows = []
    for group, keys in RAW_GROUPS:
        present = [(k, _get(v, k)) for k in keys if k in v]
        if present:
            rows.append((group, present))
    return rows
