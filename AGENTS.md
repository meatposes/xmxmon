# AGENTS.md

Orientation for an agent (or human) working on xmxmon. Read this before changing
code — several design choices look arbitrary but are forced by hardware or by
mistakes already made and corrected.

## What this is, in one paragraph

xmxmon measures Intel Arc matrix-engine (XMX) and general GPU utilization by
opening a **Level Zero metric streamer** on the device and reading hardware
counters. The single most important property: it observes the *device*, never the
*process*. It does not launch, wrap, `LD_PRELOAD`, ptrace, or otherwise attach to
the workload. That is what makes it work identically for any framework, including
workloads inside other containers you don't control.

**Do not add process attachment, injection, or wrapping.** Every prior tool in this
space failed precisely there (see [Dead ends](#dead-ends)). If a feature seems to
need it, it almost certainly belongs somewhere else.

## Repo map

| File | Role |
|---|---|
| `xmxmon.cpp` | The sampler. Single-file C++, links only `libze_loader`. Emits CSV or ndjson. Everything else is downstream of this. |
| `xmx-summary.py` | Reads a CSV, prints an XMX verdict plus utilization stats. |
| `xmxmond.py` | Daemon: one `xmxmon --json` subprocess per device, aggregates into rates/gauges, serves HTTP. |
| `xmxmon-tui.py` | Terminal UI. Thin HTTP client on the daemon — no GPU access of its own. |
| `wui.html` | Web UI, served by the daemon when enabled. Vanilla JS, SSE, canvas. No build step, no dependencies. |
| `xmxmon.yaml.example` | Tracked config template. Users copy it to `xmxmon.yaml`, which is gitignored. |
| `docker-compose.override.yml.example` | Tracked template for machine-specific Compose changes; the real override is gitignored and auto-merged by Compose. |
| `Dockerfile` | Ubuntu + Intel graphics PPA + `g++` build of the sampler. |
| `docker-compose.yml` | Daemon deployment. |
| `grafana-dashboard.json` | Import-ready dashboard; uses a datasource picker, no hardcoded UID. |

## Build and test

```sh
g++ -O2 -o xmxmon xmxmon.cpp -lze_loader     # host build
docker build -t xmxmon .                      # container build
```

There is no test suite, and a meaningful one would need a GPU. **Validate changes
against real hardware using controls**, in this order:

1. **Enumeration.** `./xmxmon --list` prints devices and time-based metric groups.
   Fails fast if the environment is wrong.
2. **Negative control.** Sample an idle device. Everything should read zero. If
   idle shows activity, the aggregation or unit handling is broken.
3. **Positive control.** Run a large fp16 matmul loop (PyTorch XPU:
   `a @ b` on 4096³ half tensors) and sample it. `XVE_INST_EXECUTED_XMX_FP16` must
   go strongly non-zero while the other precisions stay at zero. This is the
   canonical proof that per-precision resolution works end to end.
4. **Daemon round trip.** `POST /capture` → run something → `POST /capture/stop` →
   confirm `GET /captures` reports a non-zero row count and the ndjson exists.

For reference, a saturating memory copy on an Arc Pro B70 sustains ~585 GB/s
combined read+write; a fp16 matmul loop reaches roughly 7% of XVE slot capacity.
Numbers wildly away from these mean something regressed.

## Platform constraints that shape the design

These are hardware/driver facts, not choices. Don't "fix" them.

- **One metric group active per device at a time.** Sampling `MemoryProfile` and
  `VectorEngineProfile` simultaneously is impossible. Hence one `group` setting,
  not a list.
- **One streamer per device.** The daemon owns the device's counters while it
  runs; the standalone CLI will fail to open against the same GPU. This is worth
  surfacing clearly in any error path you touch.
- **Counters are device-wide.** Two workloads on one GPU are summed with no way to
  separate them. Any per-process attribution feature is not implementable this way.
- **`VectorEngineProfile` is dGPU-only.** It is absent on integrated graphics, so
  code must tolerate a device that lacks the requested group.
- **Requires `dev.xe.observation_paranoid=0`** (or `dev.i915.perf_stream_paranoid=0`
  on the older driver). Without it, `zetMetricStreamerOpen` fails. It's a host
  sysctl — a container cannot set it for you.

## Level Zero specifics that will trip you

- **`ZET_ENABLE_METRICS=1` must be set before `zeInit`**, not after. `xmxmon.cpp`
  calls `setenv(..., 0)` at the top of `main` precisely for this. Moving `zeInit`
  earlier silently disables all metrics.
- **Pick the driver that has GPU devices.** `get_devices()` iterates drivers rather
  than assuming index 0 — systems with an NPU or multiple L0 drivers otherwise
  enumerate nothing.
- **`zetMetricGroupCalculateMetricValues` returns a flat array** of
  `n_reports × n_metrics`. The loop striding by `nm` depends on that layout.
- **Metric values are typed** (`zet_typed_value_t`); always go through
  `value_as_double`. Assuming one type will produce silently wrong numbers.
- **Time-based sampling only.** `--metric-query` style per-kernel-instance mode
  rejects `VectorEngineProfile` outright. Don't spend time there.
- **The driver may round the sampling period.** The tool prints the actual period
  it was given; a request for 100 ms may come back as ~54 ms.

## Daemon architecture

`xmxmond.py` runs one `xmxmon --json` **subprocess per device** rather than linking
Level Zero itself. That isolates a crashed or wedged sampler to one device and
keeps the C++ side single-purpose.

Lifecycle details that matter:

- **Changing the sampling period restarts the child.** The period is fixed at
  streamer-open time, so `_run()` breaks its read loop and re-spawns when
  `_want_period` differs. Captures use this to switch to high-rate sampling and
  drop back afterward.
- **Captures are tee'd, not buffered.** Rows are written as they arrive, so a
  killed daemon still leaves a valid partial ndjson. Preserve this — a previous
  generation of tooling lost entire captures by flushing only at exit.
- **`snapshot()` aggregates a rolling `window_s`.** Counters become per-second
  rates (sum ÷ window); levels are averaged. Which is which is decided by the
  `GAUGES` set, and `SKIP` drops bookkeeping fields (timestamps, context IDs).
  **A metric not in `GAUGES` is treated as a counter** — add new percentage or
  frequency metrics to `GAUGES` or they will be nonsensically divided by time.

## Interpreting output — the traps

These caused real wrong conclusions during development. Encode them in any
docs, UI copy, or analysis you write.

- **A decode-only capture proves nothing about XMX.** Matrix engines light up on
  matrix-shaped work. An LLM generating one token at a time does matrix-*vector*
  products, so XMX reads zero even in backends that use it heavily for batched
  work. Always capture a batch/prefill-heavy phase before concluding a backend
  "doesn't use XMX."
- **Mixed-precision DPAS is bucketed under the wider operand.** An instruction with
  8-bit activations against 2-bit weights increments `XMX_INT8`, not `XMX_INT2`.
  `XMX_INT2` requires *both* operands at 2-bit, which essentially never happens in
  real workloads. A zero INT2 counter does not mean 2-bit data never reached the
  matrix engine.
- **Nothing here knows a theoretical maximum.** Every "peak"/"max" is empirical:
  WUI charts auto-scale to the rolling window, the TUI peak-holds since launch,
  `xmx-summary.py` reports the largest sample. A full-looking bar means "busiest
  moment observed," not "saturated." Don't add a percentage-of-peak display
  without an explicitly configured ceiling.
- **`GPU_MEMORY_BYTE_*` is VRAM only** — device-local memory, measured at the
  memory controller. Host RAM over PCIe is `SYSMEM_TRANSACTION_*`; cache traffic is
  the `L3_*` family. Don't conflate them.

## Security posture

- **The web UI and Prometheus exporter are off by default and must stay that way.**
  Neither has authentication.
- **The daemon binds loopback by default**; compose publishes to `127.0.0.1`. A
  change that exposes either surface by default is a regression, not a convenience.
- The capture API is unauthenticated too, so it inherits the same bind restriction.
- The container needs only `--device /dev/dri`. **Never add `--privileged`, host PID
  namespace, or host network to make something work** — if that seems necessary,
  the approach is wrong.

## Dead ends

Do not spend time re-attempting these; each was tried and failed for a structural
reason.

- **`xpu-smi` / `xpumcli`.** EU/XMX metrics return `N/A` — not implemented for Arc
  B-series in this driver generation. Its memory-bandwidth metrics are equally
  broken: it reported ~0 during a verified 585 GB/s load. Not a permissions issue.
  Don't cross-check against it.
- **VTune attach mode.** The GPU plugin cannot hook an already-running process by
  design; it must launch the target. Its instruction-count characterization costs
  100–200× slowdown, which is unusable alongside a real benchmark.
- **`unitrace` (pti-gpu).** Reads the right counters, but must launch the target,
  and never reliably flushed results for a long-running server. Superseded
  entirely by the streamer approach here.

## Conventions

- **Style:** match what's there. C++ is single-file with `ZE_CHECK`-wrapped calls.
  Python is stdlib-only except `PyYAML` — **do not add dependencies**; the value of
  this tool is that it drops onto a machine and runs. The web UI is vanilla JS with
  no build step; keep it that way.
- **Comments** explain constraints the code can't show (hardware limits, ordering
  requirements), not what the next line does.
- **License headers:** every source file carries `SPDX-License-Identifier: Apache-2.0`.
- **Commits:** imperative subject, body explaining *why*. Never commit personal
  hostnames, IPs, emails, network names, or captured data — `.gitignore` covers the
  compiled binary, CSVs, ndjson, and `captures/`.
- **Local operational settings are not repo settings.** `xmxmon.yaml` and
  `docker-compose.override.yml` are gitignored user-owned files, created by
  copying the tracked `.example` templates. Machine-specific values (extra
  devices, exposed ports, UI enabled) belong there and must never be committed.
  To change a default, edit the `.example` template — and keep it conservative,
  because it is what a stranger gets.
- **The daemon must survive a missing config.** A fresh clone has no
  `xmxmon.yaml`, and Docker materializes a *directory* at a missing bind-mount
  source — so config loading checks `os.path.isfile` and falls back to built-in
  defaults with a warning instead of crashing. Keep that property.

## Known gaps

- No test suite; validation is manual against hardware (see above).
- No configurable peak-bandwidth ceiling, so bandwidth can't be shown as a
  percentage of hardware capability.
- Multi-group sampling is impossible per device, so comparing e.g. stall data with
  XMX data requires two runs and manual correlation.
- The B-series XVE count is not read from the device, so slot-occupancy estimates
  in external analysis are hand-computed rather than derived.
