# Metric-group views

xmxmon started as a single-group tool: it sampled `VectorEngineProfile` and every
view — terminal, web, summary, Grafana — was written around that group's
counters. This document covers the generalization to multiple groups and how to
add another.

## Why groups matter

A Level Zero device exposes many *metric groups* (`xmxmon --list`), but the
hardware allows only **one active per device at a time**. Each group is a
different lens:

- `VectorEngineProfile` — the matrix engine (XMX) and the ALU/prep work feeding
  it. Answers "is XMX used, and how much of the work is real multiply vs operand
  unpacking".
- `ComputeBasic` — the broadest single lens: XVE pipes, both cache levels, VRAM,
  the PCIe and sysmem paths, SLM, and dispatch, all in one group. The first
  place to look when you don't yet know where time goes.
- `MemoryProfile` — the memory subsystem in depth. LLM token generation is
  memory-bound, so this is usually the group that explains decode throughput:
  bandwidth, 32B-vs-64B coalescing, L3 flow-control saturation, the copy engine.
- `DeviceCacheProfile` — the device cache (L3) and, crucially, *which client*
  drives it (load/store vs instruction cache vs sampler), plus the VRAM traffic
  behind L3 misses.

The three beyond `VectorEngineProfile` were chosen as the most useful for
profiling a compute/inference workload; the remaining groups are graphics- or
raytracing-oriented.

## How a view is selected

The daemon samples whatever `group` (or per-device `groups:` override) the config
names, and records that group in every snapshot. From there:

- **TUI** (`xmxmon-tui.py`) renders its left column from the group's `hero` spec.
- **Web UI** (`wui.html`) is fully data-driven: the daemon embeds a `view`
  (tiles + charts + raw groups) in each snapshot, and the page renders that spec.
  It knows how to draw a spec, not what any group contains.
- **Summary** (`xmx-summary.py`) calls `detect_group()` on the capture's columns,
  so an offline CSV needs no `--group` flag.
- **Grafana** — one dashboard per group, all reading the same generic
  `xmxmon_gauge` / `xmxmon_rate_per_s` / `xmxmon_derived` series.

Nothing but `xmxderive.py` knows what the groups are. That is the whole point of
the profile registry: one place to add a view, four surfaces that pick it up.

### Switching at runtime

`POST /group {"device": N, "group": G}` changes a device's group without
touching the config — the web UI's per-device dropdown and the TUI's `g` key both
call it. `Sampler.switch_group` sets `_want_group`; the read loop already breaks
and respawns the child on a group change (the same path a capture uses for a
period change), and `_spawn` **clears the rolling window** on the switch so the
next aggregation isn't a blend of two groups' metrics. It is in-memory only, so a
restart reverts to config. Constraints, both from the hardware: the switch is
device-wide (every viewer sees it) and refused mid-capture (one ndjson, one
schema). The web UI rebuilds a device's panel when it sees the group change in
the snapshot, so the charts follow.

Allowed targets are enumerated per device at startup by `enumerate_groups`
(which parses `xmxmon --list`), not hardcoded — so the menu reflects exactly what
a card supports. `xmxderive.SWITCHABLE` only sets the *preferred order* (profiled
groups first); `UNSWITCHABLE_GROUPS` (`EuStallSampling`, `TestOa`) are filtered
out because they need a different Level Zero mechanism. A switch to a group with
no profile still works — `derive`/`view` fall back to the VectorEngineProfile
profile, so the curated ratios mostly drop out but the raw counters and any
shared metrics (GPU busy, VRAM bytes) still render. The TUI picker also offers an
"apply to all devices" entry that sets every card to the next group chosen.

## The profile format (`xmxderive.py`)

`PROFILES[group]` is a dict:

```python
"MemoryProfile": {
    "title": "memory subsystem",     # shown in TUI/WUI headers
    "derive": _derive_memory,        # values -> [(label, value, unit, note)]
    "raw_groups": _RAW_MEMORY,       # [(section, [metric, ...])] raw dump
    "hero": [ ... ],                 # TUI left column (see kinds below)
    "view": { "title", "tiles", "charts" },   # web-UI spec (JSON-serialisable)
}
```

Public accessors dispatch on the group and default to `VectorEngineProfile`:
`derive(v, group)`, `raw_rows(v, group)`, `hero(group)`, `view(group)`.
`view()` also folds `raw_groups` into the returned spec for the web UI.

### `derive`

A function taking a values dict (per-second rates and averaged levels merged) and
returning `(label, value, unit, note)` tuples. Units: `"x"` ratio, `"%"`
percentage, `"/s"` rate, `"B/s"` bytes/sec, `""` plain count. Guard every metric
with `in v` (helpers `_has`, `_get`, `_sum`, `_hitrate`, `_pct`, `_bytes` do
this) so a view degrades gracefully when a counter is missing.

Derived labels are exported to Prometheus via a slug of the label
(`xmxmond._slug`), so **keep labels stable** — renaming a label renames its
`xmxmon_derived{metric=...}` series and breaks any dashboard referencing it.

### `hero` — terminal left column

An ordered list of `(kind, label, arg)`. The renderer lives in
`xmxmon-tui.py::hero_lines`. Kinds:

| kind | arg | renders |
|---|---|---|
| `pct` | metric | gauge %, bar with peak marker |
| `rate` | metric | counter, `si/s`, peak-scaled bar, `(idle)` when zero |
| `rwgb` | `(read, write)` | two byte counters as `R… W… GB/s` |
| `rwtx64` | `(read, write)` | two 64B-transaction counters → GB/s |
| `freq` | metric | gauge → MHz |
| `xmxgroup` | `[(sub, metric), …]` | shared-scale multi-rate block with idle marks |

### `view` — web tiles and charts

JSON-serialisable, sent to the browser verbatim.

- `tiles`: headline numbers. `{label, kind, key}` (or `keys` for `ratemax`).
  Kinds: `pct`, `pct1`, `rate`, `gbps`, `tx64gbps`, `freq`, `ratemax`.
- `charts`: `{id, title, kind, fmt, scale?, max?, series}`.
  `kind` is `"rates"` or `"gauges"` (which snapshot map to read). `fmt` is
  `"si"`, `"pct"`, or `"num"` (y-axis / hover formatter). `scale` multiplies only
  the displayed number, not the plotted shape (e.g. `1e-9` to show bytes as GB).
  `series` is `[[metric, legend, colour_index], …]`.

## Adding a group

1. Confirm the metric names with
   `xmxmon --device N --group <Group> --list-metrics`.
2. Add a `_derive_<group>` function and `_RAW_<group>` list in `xmxderive.py`.
3. Add a `PROFILES[<group>]` entry with `hero` and `view`.
4. If the group has new percentage/level metrics, add them to `PERCENT` (or make
   sure they match `is_percent`'s suffix rules) so they are averaged, not summed.
5. Add a `detect_group` signature if offline captures should auto-detect it.
6. Generate a Grafana dashboard (see `grafana-dashboard-*.json`) that reads the
   generic `xmxmon_*` series.

No changes to the daemon, TUI, web UI, or summary are required — they all read
the profile.

## Validation

Same controls as the rest of the tool (see `AGENTS.md`): enumerate, sample an
idle device (everything reads ~zero), then sample under a known load and confirm
the group's headline metric moves. For `MemoryProfile`, a saturating memory copy
should drive `GPU_MEMORY_BYTE_*` toward the card's ceiling (~585 GB/s combined on
a B70) with high 64B coalescing; for `DeviceCacheProfile`, an L3-resident working
set should show a high hit rate with little GTI/VRAM traffic behind it.
