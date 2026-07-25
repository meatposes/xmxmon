# Copyright 2026 the xmxmon authors
# SPDX-License-Identifier: Apache-2.0
"""Derived overhead metrics and per-group view specs.

Shared by the daemon, TUI, web UI, and offline summary so every surface agrees.

Raw counters answer "what happened". The derived ratios answer "how much of it
was useful work". Both are grouped into **profiles**, one per Level Zero metric
group, because only one group can be sampled per device at a time (a hardware
limit) — so a device is only ever showing one group's worth of metrics, and the
view has to match whichever group is active.

A profile carries everything a view needs for its group:

  derive       function(values) -> list of (label, value, unit, note) ratios
  raw_groups   [(section, [metric, ...])] for the raw counter dump
  hero         terminal-UI left-column spec (see render in xmxmon-tui.py)
  view         {title, tiles, charts} the web UI renders generically

Every ratio divides two counters of the same kind, so it reads identically for
per-second rates (live) or totals over a capture (offline). Absolute quantities
carry a unit and only mean something when the inputs are rates. Metrics absent
from the active group are skipped rather than zeroed, so each view adapts to
whatever is actually being sampled.
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

# The GPU counts memory traffic to system/PCIe in fixed 64-byte transactions;
# device-local (VRAM) traffic is reported directly in bytes.
TX_BYTES = 64

# Metrics that are already levels (percentages, frequencies, pre-computed
# bandwidth) and must be AVERAGED over a window, never summed as per-second
# counters. The daemon and the offline summary both consume this so their
# aggregation agrees. Percentages named *_UTILIZATION / *_RATE and any
# XVE_STALL_* also match by suffix/prefix in is_percent().
PERCENT = {
    "GPU_BUSY", "XVE_ACTIVE", "XVE_STALL", "XVE_THREADS_OCCUPANCY_ALL",
    "XVE_MULTIPLE_PIPE_ACTIVE", "XVE_PIPE_ALU0_AND_ALU1_ACTIVE",
    "XVE_PIPE_ALU0_AND_ALU2_ACTIVE", "XVE_SHARED_FUNCTION_ACCESS_HOLD",
    "GPGPU_DISPATCH",
    "L3_BUSY", "L3_STALL", "L3_INPUT_AVAILABLE", "L3_OUTPUT_READY",
    "L3_SUPERQ_FULL", "GPU_MEMORY_REQUEST_QUEUE_FULL",
    "LOAD_STORE_CACHE_INPUT_AVAILABLE", "LOAD_STORE_CACHE_OUTPUT_READY",
    "COPY_ENGINE_REQUEST_STALL",
    "COMMAND_PARSER_COMPUTE_ENGINE_BUSY", "COMMAND_PARSER_RENDER_ENGINE_BUSY",
    "COMMAND_PARSER_COPY_ENGINE_BUSY",
    "AvgGpuCoreFrequencyMHz", "CoreFrequencyMHz", "ResultUncertainty",
    "XVE_INST_EXECUTED_ALU0_ALL_UTILIZATION",
    "XVE_INST_EXECUTED_ALU1_ALL_UTILIZATION",
    "XVE_INST_EXECUTED_ALU2_ALL_UTILIZATION",
}


def is_percent(metric):
    """True if a metric is a level (averaged), not a per-second counter."""
    return (metric in PERCENT or metric.startswith("XVE_STALL_")
            or metric.endswith("_UTILIZATION") or metric.endswith("_RATE"))


def _has(v, keys):
    return any(k in v for k in keys)


def _sum(v, keys):
    return sum(float(v.get(k, 0) or 0) for k in keys)


def _get(v, k):
    return float(v.get(k, 0) or 0)


# --- derived-row helpers ---------------------------------------------------
# Each appends (label, value, unit, note) when its inputs are present. Units:
# "x" ratio, "%" percentage, "/s" rate, "B/s" bytes/sec, "" plain count.

def _hitrate(out, v, hits, total, label, note):
    """hits / total as a percentage; total may be a direct count or a miss."""
    if hits not in v:
        return
    h = _get(v, hits)
    t = _get(v, total) if total in v else 0.0
    denom = t if total.endswith(("ACCESS", "READ")) else h + t
    if denom > 0:
        out.append((label, 100.0 * h / denom, "%", note))


def _pct(out, v, key, label, note=None):
    if key in v:
        out.append((label, _get(v, key), "%", note))


def _bytes(out, v, label, keys, scale, note=None):
    """Sum byte/transaction counters and report as bytes/sec (scale to bytes)."""
    if _has(v, keys):
        out.append((label, _sum(v, keys) * scale, "B/s", note))


# --- per-group derive functions -------------------------------------------

def _derive_vep(v):
    """VectorEngineProfile: matrix-engine efficiency and operand-prep tax."""
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


def _derive_compute(v):
    """ComputeBasic: where the workload spends time across compute + memory.

    The one group that sees XVE pipes, both cache levels, VRAM, and the PCIe /
    sysmem paths at once — so its ratios frame the whole pipeline.
    """
    out = []
    issued = _get(v, "XVE_INST_ISSUED_ALL")
    if "XVE_INST_EXECUTED_SEND_ALL" in v and issued > 0:
        out.append(("send / issued",
                    100.0 * _get(v, "XVE_INST_EXECUTED_SEND_ALL") / issued, "%",
                    "share of issue slots spent on load/store"))
    _pct(out, v, "XVE_MULTIPLE_PIPE_ACTIVE", "multi-pipe active",
         "instruction-level parallelism")
    _pct(out, v, "GPGPU_DISPATCH", "dispatch overhead",
         "time spent launching threads onto the XVEs")
    _pct(out, v, "COMMAND_PARSER_COMPUTE_ENGINE_BUSY", "compute engine busy",
         "context loaded and active on the compute queue")

    _hitrate(out, v, "LOAD_STORE_CACHE_HIT", "LOAD_STORE_CACHE_ACCESS",
             "L1 hit rate", "load/store cache; low = spilling to L3")
    _hitrate(out, v, "L3_HIT", "L3_MISS", "L3 hit rate",
             "low = operands spilling to VRAM")
    ih, im = _get(v, "ICACHE_HIT"), _get(v, "ICACHE_MISS")
    if "ICACHE_MISS" in v and ih + im > 0:
        out.append(("icache miss", 100.0 * im / (ih + im), "%",
                    "high = oversized or spilling kernels"))

    _bytes(out, v, "VRAM read", ["GPU_MEMORY_BYTE_READ"], 1, "device-local")
    _bytes(out, v, "VRAM write", ["GPU_MEMORY_BYTE_WRITE"], 1, "device-local")
    _bytes(out, v, "PCIe host→GPU",
           ["HOST_TO_GPUMEM_TRANSACTION_READ", "HOST_TO_GPUMEM_TRANSACTION_WRITE"],
           TX_BYTES, "downstream: weight / KV upload")
    _bytes(out, v, "GPU→sysmem",
           ["SYSMEM_TRANSACTION_READ", "SYSMEM_TRANSACTION_WRITE"],
           TX_BYTES, "upstream: host readback / eviction")
    _bytes(out, v, "SLM traffic", ["SLM_BYTE_READ", "SLM_BYTE_WRITE"], 1,
           "shared local memory")

    if "COMMAND_PARSER_COMPUTE_ENGINE_DISPATCH_KERNEL_COUNT" in v:
        out.append(("kernel dispatches",
                    _get(v, "COMMAND_PARSER_COMPUTE_ENGINE_DISPATCH_KERNEL_COUNT"),
                    "/s", "launch rate"))
    return out


def _derive_memory(v):
    """MemoryProfile: the memory subsystem in depth — the decode bottleneck.

    Token generation is memory-bound, so coalescing, cache hit rates, and
    request-queue saturation here explain far more than XVE counters do.
    """
    out = []
    _bytes(out, v, "VRAM read", ["GPU_MEMORY_BYTE_READ"], 1, None)
    _bytes(out, v, "VRAM write", ["GPU_MEMORY_BYTE_WRITE"], 1, None)

    # Fraction of transactions that are full 64B rather than 32B — the practical
    # measure of memory coalescing. Low = scattered access wasting bandwidth.
    for tag, r32, r64 in (("read", "GPU_MEMORY_32B_TRANSACTION_READ",
                           "GPU_MEMORY_64B_TRANSACTION_READ"),
                          ("write", "GPU_MEMORY_32B_TRANSACTION_WRITE",
                           "GPU_MEMORY_64B_TRANSACTION_WRITE")):
        if r64 in v:
            t32, t64 = _get(v, r32), _get(v, r64)
            if t32 + t64 > 0:
                out.append((f"{tag} coalescing", 100.0 * t64 / (t32 + t64), "%",
                            "share of full 64B transactions; low = scattered"))

    _hitrate(out, v, "LOAD_STORE_CACHE_HIT", "LOAD_STORE_CACHE_ACCESS",
             "L1 hit rate", "load/store cache")
    _hitrate(out, v, "L3_HIT", "L3_MISS", "L3 hit rate",
             "low = misses driving VRAM traffic")

    _pct(out, v, "L3_STALL", "L3 stall", "cache bank stalled")
    _pct(out, v, "L3_SUPERQ_FULL", "L3 superq full",
         "all request slots waiting on data return — memory saturated")
    _pct(out, v, "GPU_MEMORY_REQUEST_QUEUE_FULL", "mem queue full",
         "controller request queue over threshold")
    _pct(out, v, "COPY_ENGINE_REQUEST_STALL", "copy engine stall",
         "blit/copy path blocked on memory")

    # Partial writes don't fill a cache subsector: read-modify-write overhead.
    acc = _get(v, "LOAD_STORE_CACHE_ACCESS")
    if "LOAD_STORE_CACHE_PARTIAL_WRITE_COUNT" in v and acc > 0:
        out.append(("L1 partial writes",
                    100.0 * _get(v, "LOAD_STORE_CACHE_PARTIAL_WRITE_COUNT") / acc,
                    "%", "sub-sector writes forcing read-modify-write"))

    # SLM bank conflicts serialize otherwise-parallel accesses.
    sa = _get(v, "SLM_ACCESS_COUNT")
    if "SLM_BANK_CONFLICT_COUNT" in v and sa > 0:
        out.append(("SLM bank conflicts",
                    100.0 * _get(v, "SLM_BANK_CONFLICT_COUNT") / sa, "%",
                    "conflicting shared-local accesses serialized"))

    _bytes(out, v, "PCIe host→GPU",
           ["HOST_TO_GPUMEM_TRANSACTION_READ", "HOST_TO_GPUMEM_TRANSACTION_WRITE"],
           TX_BYTES, "downstream")
    _bytes(out, v, "GPU→sysmem",
           ["SYSMEM_TRANSACTION_READ", "SYSMEM_TRANSACTION_WRITE"],
           TX_BYTES, "upstream")
    return out


def _derive_cache(v):
    """DeviceCacheProfile: L3 behaviour and who is driving its traffic.

    Attributes L3 requests to their client (load/store vs instruction cache vs
    sampler) and exposes the VRAM traffic behind L3 misses.
    """
    out = []
    _hitrate(out, v, "L3_HIT", "L3_MISS", "L3 hit rate",
             "overall device-cache hit rate")

    # L3 read requests attributed to each client, as a share of the total —
    # answers "what is filling my L3".
    clients = [("load/store", "LOAD_STORE_CACHE_L3_READ"),
               ("instruction", "ICACHE_L3_READ"),
               ("sampler", "SAMPLER_L3_READ")]
    total = sum(_get(v, k) for _, k in clients if k in v)
    if total > 0:
        for name, k in clients:
            if k in v:
                out.append((f"L3 from {name}", 100.0 * _get(v, k) / total, "%",
                            "share of L3 read requests"))

    # Hit rate of the load/store client specifically against Device Cache.
    lsr = _get(v, "LOAD_STORE_CACHE_L3_READ")
    if "LOAD_STORE_CACHE_L3_HIT" in v and lsr > 0:
        out.append(("load/store L3 hit",
                    100.0 * _get(v, "LOAD_STORE_CACHE_L3_HIT") / lsr, "%",
                    "how often load/store misses still hit L3"))

    _pct(out, v, "L3_BUSY", "L3 busy", "request queue non-empty")
    _pct(out, v, "L3_SUPERQ_FULL", "L3 superq full",
         "all slots waiting on data — downstream saturated")
    _pct(out, v, "L3_STALL", "L3 stall", "cache bank stalled")

    _bytes(out, v, "L3→VRAM read", ["GPU_MEMORY_L3_READ"], TX_BYTES,
           "misses fetched from VRAM")
    _bytes(out, v, "L3→VRAM write", ["GPU_MEMORY_L3_WRITE"], TX_BYTES,
           "evictions written to VRAM")
    return out


# --- raw counter groupings -------------------------------------------------

_RAW_VEP = [
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

_RAW_COMPUTE = [
    ("vector pipes", ["XVE_INST_EXECUTED_ALU0_ALL", "XVE_INST_EXECUTED_ALU1_ALL",
                      "XVE_INST_EXECUTED_ALU2_ALL", "XVE_INST_EXECUTED_SEND_ALL",
                      "XVE_INST_ISSUED_ALL"]),
    ("L1 / SLM", ["LOAD_STORE_CACHE_BYTE_READ", "LOAD_STORE_CACHE_BYTE_WRITE",
                  "LOAD_STORE_CACHE_ACCESS", "LOAD_STORE_CACHE_HIT",
                  "SLM_BYTE_READ", "SLM_BYTE_WRITE"]),
    ("L3 / VRAM", ["L3_HIT", "L3_MISS", "L3_READ", "L3_WRITE",
                   "GPU_MEMORY_BYTE_READ", "GPU_MEMORY_BYTE_WRITE", "TLB_MISS"]),
    ("PCIe / sysmem", ["HOST_TO_GPUMEM_TRANSACTION_READ",
                       "HOST_TO_GPUMEM_TRANSACTION_WRITE",
                       "SYSMEM_TRANSACTION_READ", "SYSMEM_TRANSACTION_WRITE"]),
    ("dispatch", ["COMMAND_PARSER_COMPUTE_ENGINE_DISPATCH_KERNEL_COUNT",
                  "GPGPU_THREADGROUP_COUNT", "ASYNC_GPGPU_THREADGROUP_COUNT"]),
]

_RAW_MEMORY = [
    ("VRAM bytes", ["GPU_MEMORY_BYTE_READ", "GPU_MEMORY_BYTE_WRITE"]),
    ("VRAM transactions", ["GPU_MEMORY_32B_TRANSACTION_READ",
                           "GPU_MEMORY_64B_TRANSACTION_READ",
                           "GPU_MEMORY_32B_TRANSACTION_WRITE",
                           "GPU_MEMORY_64B_TRANSACTION_WRITE"]),
    ("L3", ["L3_HIT", "L3_MISS", "L3_READ", "L3_WRITE", "L3_ATOMIC_ACCESS"]),
    ("L1 load/store", ["LOAD_STORE_CACHE_BYTE_READ", "LOAD_STORE_CACHE_BYTE_WRITE",
                       "LOAD_STORE_CACHE_ACCESS", "LOAD_STORE_CACHE_HIT",
                       "LOAD_STORE_CACHE_PARTIAL_WRITE_COUNT"]),
    ("SLM", ["SLM_BYTE_READ", "SLM_BYTE_WRITE", "SLM_ACCESS_COUNT",
             "SLM_BANK_CONFLICT_COUNT"]),
    ("copy engine", ["COPY_ENGINE_READ_REQUEST", "COPY_ENGINE_WRITE_REQUEST"]),
    ("PCIe / sysmem", ["HOST_TO_GPUMEM_TRANSACTION_READ",
                       "HOST_TO_GPUMEM_TRANSACTION_WRITE",
                       "SYSMEM_TRANSACTION_READ", "SYSMEM_TRANSACTION_WRITE"]),
]

_RAW_CACHE = [
    ("L3 core", ["L3_HIT", "L3_MISS", "L3_READ", "L3_WRITE", "L3_ATOMIC_ACCESS"]),
    ("L3 by client", ["LOAD_STORE_CACHE_L3_READ", "LOAD_STORE_CACHE_L3_HIT",
                      "LOAD_STORE_CACHE_L3_WRITE", "ICACHE_L3_READ",
                      "ICACHE_L3_HIT", "SAMPLER_L3_READ", "SAMPLER_L3_HIT"]),
    ("graphics clients", ["COLOR_L3_ACCESS", "COLOR_L3_HIT", "Z_L3_ACCESS",
                          "Z_L3_HIT", "AMFS_L3_ACCESS", "AMFS_L3_HIT"]),
    ("GTI / VRAM", ["GPU_MEMORY_READ", "GPU_MEMORY_WRITE",
                    "GPU_MEMORY_L3_READ", "GPU_MEMORY_L3_WRITE"]),
]


# --- view specs (terminal hero + web tiles/charts) -------------------------
# hero items drive the TUI left column; the renderer lives in xmxmon-tui.py.
#   (kind, label, arg)  kinds:
#     pct     arg=key            gauge %, bar with peak marker
#     rate    arg=key            counter, si/s
#     rwgb    arg=(rd, wr)       two byte counters -> "R.. W.. GB/s"
#     rwtx64  arg=(rd, wr)       two 64B-transaction counters -> GB/s
#     freq    arg=key            gauge -> MHz
#     xmxgroup arg=[(sub,key)]   shared-scale multi-rate block with idle marks

_XMX_LABELLED = [("INT2", "XVE_INST_EXECUTED_XMX_INT2"),
                 ("INT4", "XVE_INST_EXECUTED_XMX_INT4"),
                 ("INT8", "XVE_INST_EXECUTED_XMX_INT8"),
                 ("FP16", "XVE_INST_EXECUTED_XMX_FP16"),
                 ("BF16", "XVE_INST_EXECUTED_XMX_BF16")]

PROFILES = {
    "VectorEngineProfile": {
        "title": "matrix engine (XMX)",
        "derive": _derive_vep,
        "raw_groups": _RAW_VEP,
        "hero": [
            ("pct", "busy", "GPU_BUSY"),
            ("pct", "XVE act", "XVE_ACTIVE"),
            ("pct", "occupancy", "XVE_THREADS_OCCUPANCY_ALL"),
            ("xmxgroup", "XMX", _XMX_LABELLED),
            ("rwgb", "mem", ("GPU_MEMORY_BYTE_READ", "GPU_MEMORY_BYTE_WRITE")),
            ("freq", "freq", "AvgGpuCoreFrequencyMHz"),
        ],
        "view": {
            "title": "matrix engine (XMX)",
            "tiles": [
                {"label": "GPU busy", "kind": "pct", "key": "GPU_BUSY"},
                {"label": "XVE active", "kind": "pct1", "key": "XVE_ACTIVE"},
                {"label": "XMX peak", "kind": "ratemax", "keys": XMX},
                {"label": "Mem read", "kind": "gbps", "key": "GPU_MEMORY_BYTE_READ"},
                {"label": "Freq", "kind": "freq", "key": "AvgGpuCoreFrequencyMHz"},
            ],
            "charts": [
                {"id": "xmx", "title": "XMX instructions / s", "kind": "rates",
                 "fmt": "si", "series": [[k, lbl, i] for i, (lbl, k)
                                         in enumerate(_XMX_LABELLED)]},
                {"id": "util", "title": "Utilization %", "kind": "gauges",
                 "fmt": "pct", "max": 100,
                 "series": [["GPU_BUSY", "GPU busy", 0],
                            ["XVE_ACTIVE", "XVE active", 1],
                            ["XVE_THREADS_OCCUPANCY_ALL", "Occupancy", 2]]},
                {"id": "mem", "title": "Memory bandwidth GB/s", "kind": "rates",
                 "fmt": "num", "scale": 1e-9,
                 "series": [["GPU_MEMORY_BYTE_READ", "read", 0],
                            ["GPU_MEMORY_BYTE_WRITE", "write", 1]]},
            ],
        },
    },
    "ComputeBasic": {
        "title": "compute overview",
        "derive": _derive_compute,
        "raw_groups": _RAW_COMPUTE,
        "hero": [
            ("pct", "busy", "GPU_BUSY"),
            ("pct", "XVE act", "XVE_ACTIVE"),
            ("pct", "occupancy", "XVE_THREADS_OCCUPANCY_ALL"),
            ("pct", "dispatch", "GPGPU_DISPATCH"),
            ("rwgb", "VRAM", ("GPU_MEMORY_BYTE_READ", "GPU_MEMORY_BYTE_WRITE")),
            ("rwtx64", "PCIe", ("HOST_TO_GPUMEM_TRANSACTION_READ",
                                "HOST_TO_GPUMEM_TRANSACTION_WRITE")),
            ("rwtx64", "sysmem", ("SYSMEM_TRANSACTION_READ",
                                  "SYSMEM_TRANSACTION_WRITE")),
            ("freq", "freq", "AvgGpuCoreFrequencyMHz"),
        ],
        "view": {
            "title": "compute overview",
            "tiles": [
                {"label": "GPU busy", "kind": "pct", "key": "GPU_BUSY"},
                {"label": "XVE active", "kind": "pct1", "key": "XVE_ACTIVE"},
                {"label": "Occupancy", "kind": "pct", "key": "XVE_THREADS_OCCUPANCY_ALL"},
                {"label": "VRAM read", "kind": "gbps", "key": "GPU_MEMORY_BYTE_READ"},
                {"label": "Dispatch", "kind": "pct", "key": "GPGPU_DISPATCH"},
                {"label": "Freq", "kind": "freq", "key": "AvgGpuCoreFrequencyMHz"},
            ],
            "charts": [
                {"id": "util", "title": "Utilization %", "kind": "gauges",
                 "fmt": "pct", "max": 100,
                 "series": [["GPU_BUSY", "GPU busy", 0],
                            ["XVE_ACTIVE", "XVE active", 1],
                            ["XVE_THREADS_OCCUPANCY_ALL", "Occupancy", 2],
                            ["XVE_STALL", "XVE stall", 3],
                            ["GPGPU_DISPATCH", "Dispatch", 4]]},
                {"id": "vram", "title": "VRAM bandwidth GB/s", "kind": "rates",
                 "fmt": "num", "scale": 1e-9,
                 "series": [["GPU_MEMORY_BYTE_READ", "read", 0],
                            ["GPU_MEMORY_BYTE_WRITE", "write", 1]]},
                {"id": "pcie", "title": "PCIe & sysmem GB/s", "kind": "rates",
                 "fmt": "num", "scale": 64e-9,
                 "series": [["HOST_TO_GPUMEM_TRANSACTION_READ", "host→gpu rd", 0],
                            ["HOST_TO_GPUMEM_TRANSACTION_WRITE", "host→gpu wr", 1],
                            ["SYSMEM_TRANSACTION_READ", "sysmem rd", 2],
                            ["SYSMEM_TRANSACTION_WRITE", "sysmem wr", 3]]},
                {"id": "cache", "title": "L1 / SLM bytes GB/s", "kind": "rates",
                 "fmt": "num", "scale": 1e-9,
                 "series": [["LOAD_STORE_CACHE_BYTE_READ", "L1 read", 0],
                            ["LOAD_STORE_CACHE_BYTE_WRITE", "L1 write", 1],
                            ["SLM_BYTE_READ", "SLM read", 2],
                            ["SLM_BYTE_WRITE", "SLM write", 3]]},
            ],
        },
    },
    "MemoryProfile": {
        "title": "memory subsystem",
        "derive": _derive_memory,
        "raw_groups": _RAW_MEMORY,
        "hero": [
            ("pct", "busy", "GPU_BUSY"),
            ("rwgb", "VRAM", ("GPU_MEMORY_BYTE_READ", "GPU_MEMORY_BYTE_WRITE")),
            ("pct", "L3 busy", "L3_BUSY"),
            ("pct", "L3 superq", "L3_SUPERQ_FULL"),
            ("pct", "mem q full", "GPU_MEMORY_REQUEST_QUEUE_FULL"),
            ("rwtx64", "PCIe", ("HOST_TO_GPUMEM_TRANSACTION_READ",
                                "HOST_TO_GPUMEM_TRANSACTION_WRITE")),
            ("rwtx64", "sysmem", ("SYSMEM_TRANSACTION_READ",
                                  "SYSMEM_TRANSACTION_WRITE")),
            ("freq", "freq", "AvgGpuCoreFrequencyMHz"),
        ],
        "view": {
            "title": "memory subsystem",
            "tiles": [
                {"label": "GPU busy", "kind": "pct", "key": "GPU_BUSY"},
                {"label": "VRAM read", "kind": "gbps", "key": "GPU_MEMORY_BYTE_READ"},
                {"label": "VRAM write", "kind": "gbps", "key": "GPU_MEMORY_BYTE_WRITE"},
                {"label": "L3 busy", "kind": "pct", "key": "L3_BUSY"},
                {"label": "Mem q full", "kind": "pct", "key": "GPU_MEMORY_REQUEST_QUEUE_FULL"},
                {"label": "Freq", "kind": "freq", "key": "AvgGpuCoreFrequencyMHz"},
            ],
            "charts": [
                {"id": "vram", "title": "VRAM bandwidth GB/s", "kind": "rates",
                 "fmt": "num", "scale": 1e-9,
                 "series": [["GPU_MEMORY_BYTE_READ", "read", 0],
                            ["GPU_MEMORY_BYTE_WRITE", "write", 1]]},
                {"id": "txmix", "title": "VRAM transactions / s (32B vs 64B)",
                 "kind": "rates", "fmt": "si",
                 "series": [["GPU_MEMORY_64B_TRANSACTION_READ", "64B read", 0],
                            ["GPU_MEMORY_32B_TRANSACTION_READ", "32B read", 1],
                            ["GPU_MEMORY_64B_TRANSACTION_WRITE", "64B write", 2],
                            ["GPU_MEMORY_32B_TRANSACTION_WRITE", "32B write", 3]]},
                {"id": "l3flow", "title": "L3 flow control %", "kind": "gauges",
                 "fmt": "pct", "max": 100,
                 "series": [["L3_BUSY", "busy", 0], ["L3_STALL", "stall", 1],
                            ["L3_SUPERQ_FULL", "superq full", 2],
                            ["GPU_MEMORY_REQUEST_QUEUE_FULL", "mem q full", 3],
                            ["L3_INPUT_AVAILABLE", "input avail", 4]]},
                {"id": "l1slm", "title": "L1 / SLM bytes GB/s", "kind": "rates",
                 "fmt": "num", "scale": 1e-9,
                 "series": [["LOAD_STORE_CACHE_BYTE_READ", "L1 read", 0],
                            ["LOAD_STORE_CACHE_BYTE_WRITE", "L1 write", 1],
                            ["SLM_BYTE_READ", "SLM read", 2],
                            ["SLM_BYTE_WRITE", "SLM write", 3]]},
                {"id": "pcie", "title": "PCIe & sysmem GB/s", "kind": "rates",
                 "fmt": "num", "scale": 64e-9,
                 "series": [["HOST_TO_GPUMEM_TRANSACTION_READ", "host→gpu rd", 0],
                            ["HOST_TO_GPUMEM_TRANSACTION_WRITE", "host→gpu wr", 1],
                            ["SYSMEM_TRANSACTION_READ", "sysmem rd", 2],
                            ["SYSMEM_TRANSACTION_WRITE", "sysmem wr", 3]]},
                {"id": "copy", "title": "Copy engine requests / s", "kind": "rates",
                 "fmt": "si",
                 "series": [["COPY_ENGINE_READ_REQUEST", "read", 0],
                            ["COPY_ENGINE_WRITE_REQUEST", "write", 1]]},
            ],
        },
    },
    "DeviceCacheProfile": {
        "title": "device cache (L3)",
        "derive": _derive_cache,
        "raw_groups": _RAW_CACHE,
        "hero": [
            ("pct", "busy", "GPU_BUSY"),
            ("pct", "L3 busy", "L3_BUSY"),
            ("pct", "L3 superq", "L3_SUPERQ_FULL"),
            ("pct", "L3 stall", "L3_STALL"),
            ("rate", "L3 rd", "L3_READ"),
            ("rate", "L3 wr", "L3_WRITE"),
            ("rate", "GTI rd", "GPU_MEMORY_READ"),
            ("rate", "GTI wr", "GPU_MEMORY_WRITE"),
            ("freq", "freq", "AvgGpuCoreFrequencyMHz"),
        ],
        "view": {
            "title": "device cache (L3)",
            "tiles": [
                {"label": "GPU busy", "kind": "pct", "key": "GPU_BUSY"},
                {"label": "L3 busy", "kind": "pct", "key": "L3_BUSY"},
                {"label": "L3 read", "kind": "rate", "key": "L3_READ"},
                {"label": "L3 write", "kind": "rate", "key": "L3_WRITE"},
                {"label": "Superq full", "kind": "pct", "key": "L3_SUPERQ_FULL"},
                {"label": "Freq", "kind": "freq", "key": "AvgGpuCoreFrequencyMHz"},
            ],
            "charts": [
                {"id": "l3acc", "title": "L3 accesses / s", "kind": "rates",
                 "fmt": "si",
                 "series": [["L3_HIT", "hit", 0], ["L3_MISS", "miss", 1],
                            ["L3_READ", "read", 2], ["L3_WRITE", "write", 3]]},
                {"id": "l3flow", "title": "L3 flow control %", "kind": "gauges",
                 "fmt": "pct", "max": 100,
                 "series": [["L3_BUSY", "busy", 0], ["L3_STALL", "stall", 1],
                            ["L3_SUPERQ_FULL", "superq full", 2],
                            ["L3_INPUT_AVAILABLE", "input avail", 3],
                            ["L3_OUTPUT_READY", "output ready", 4]]},
                {"id": "l3client", "title": "L3 reads by client / s", "kind": "rates",
                 "fmt": "si",
                 "series": [["LOAD_STORE_CACHE_L3_READ", "load/store", 0],
                            ["ICACHE_L3_READ", "instruction", 1],
                            ["SAMPLER_L3_READ", "sampler", 2],
                            ["AMFS_L3_ACCESS", "amfs", 3]]},
                {"id": "gti", "title": "L3 → VRAM traffic / s", "kind": "rates",
                 "fmt": "si",
                 "series": [["GPU_MEMORY_READ", "GTI read", 0],
                            ["GPU_MEMORY_WRITE", "GTI write", 1],
                            ["GPU_MEMORY_L3_READ", "miss→VRAM rd", 2],
                            ["GPU_MEMORY_L3_WRITE", "evict→VRAM wr", 3]]},
            ],
        },
    },
}


# Groups a UI may switch a device to at runtime. Each has a coherent view (the
# four profiled groups) or renders usefully through the VEP profile's derive
# (VectorEngineStalls -> stall breakdown). Ordered for cycling in the TUI.
SWITCHABLE = ["VectorEngineProfile", "ComputeBasic", "MemoryProfile",
              "DeviceCacheProfile", "VectorEngineStalls"]


def _profile(group):
    """Return the profile for a group, defaulting to VectorEngineProfile.

    XVE-stall captures reuse the VEP profile: its derive already renders the
    XVE_STALL_* breakdown, and no dedicated stalls view is needed.
    """
    if group == "VectorEngineStalls":
        return PROFILES["VectorEngineProfile"]
    return PROFILES.get(group, PROFILES["VectorEngineProfile"])


def derive(v, group=None):
    """Compute derived metrics for a values dict under the given metric group.

    Returns an ordered list of (label, value, unit, note). `unit` is "x" ratio,
    "%" percentage, "/s" rate, "B/s" bytes/sec, "" plain count. Passing no group
    yields the VectorEngineProfile ratios (backwards compatible).
    """
    return _profile(group)["derive"](v)


def raw_rows(v, group=None):
    """Ordered (section, [(metric, value)]) for the raw dump; skips absent keys."""
    rows = []
    for section, keys in _profile(group)["raw_groups"]:
        present = [(k, _get(v, k)) for k in keys if k in v]
        if present:
            rows.append((section, present))
    return rows


def hero(group=None):
    """Terminal-UI left-column spec for the group."""
    return _profile(group)["hero"]


def view(group=None):
    """Web-UI view spec ({title, tiles, charts, raw_groups}) for the group."""
    p = _profile(group)
    spec = dict(p["view"])
    spec["raw_groups"] = [[section, list(keys)] for section, keys
                          in p["raw_groups"]]
    return spec


# Backwards-compatible alias: older callers imported RAW_GROUPS directly.
RAW_GROUPS = _RAW_VEP


# Signature metrics unique to each group, so an offline capture can be matched
# to its profile without the group being recorded per row.
_SIGNATURES = [
    ("VectorEngineStalls", ("XVE_STALL_ALLOC",)),
    ("VectorEngineProfile", ("XVE_INST_EXECUTED_XMX_FP16",)),
    ("MemoryProfile", ("GPU_MEMORY_32B_TRANSACTION_READ",)),
    ("DeviceCacheProfile", ("ICACHE_L3_READ", "GPU_MEMORY_READ")),
    ("ComputeBasic", ("GPGPU_DISPATCH",)),
]


def detect_group(cols):
    """Best-guess metric group from the column names of a capture."""
    cols = set(cols)
    for group, sig in _SIGNATURES:
        if any(s in cols for s in sig):
            return group
    return "VectorEngineProfile"
