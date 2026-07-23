# xmxmon

Measure real **XMX** (matrix engine) utilization on Intel Arc GPUs, per precision,
while any workload runs.

Intel's tooling makes this surprisingly hard. `xpu-smi`/`xpumcli` don't implement
the EU/XMX metrics for Arc B-series. VTune's GPU plugin can't attach to an
already-running process, and its instruction-count mode costs 100-200x slowdown.
`unitrace` reads the right counters but has to launch the target itself.

xmxmon takes a different approach: it opens a Level Zero **metric streamer** on the
device and samples hardware counters device-wide. It never launches, wraps, injects
into, or even knows about the workload process. That makes it backend-agnostic —
it works the same for any framework that touches the GPU (SYCL, PyTorch XPU,
OpenVINO, oneDNN, your own Level Zero code), including ones running inside other
containers.

The counters that matter:

```
XVE_INST_EXECUTED_XMX_INT2    XVE_INST_EXECUTED_XMX_FP16
XVE_INST_EXECUTED_XMX_INT4    XVE_INST_EXECUTED_XMX_BF16
XVE_INST_EXECUTED_XMX_INT8
```

plus `XVE_ACTIVE`, per-ALU-pipe instruction counts, occupancy, L3, and memory
bandwidth — all from the same `VectorEngineProfile` group.

Developed against Arc Pro B70 and B50. Other Arc / Data Center GPU Max parts that
expose `VectorEngineProfile` should work; check with `xmxmon --list`.

## Requirements

- An Intel GPU with the compute runtime installed (`libze1`, `libze-intel-gpu1`)
- `intel-metrics-discovery` and `intel-metrics-library`
- Performance counters unlocked for non-root:
  ```sh
  sudo sysctl -w dev.xe.observation_paranoid=0     # Xe driver
  sudo sysctl -w dev.i915.perf_stream_paranoid=0   # i915 driver
  ```
  Persist with a file in `/etc/sysctl.d/`. Without this, opening the streamer fails.
- To build on the host: `g++` and `libze-dev`. The container image handles all of
  the above except the sysctl, which is a host setting.

## Quick start (container)

```sh
docker build -t xmxmon .

# What devices and metric groups do I have?
docker run --rm --device /dev/dri xmxmon --list

# Sample device 0 for 60s while your workload runs, then summarize:
docker run --rm --device /dev/dri -v "$PWD:/data" xmxmon \
    --device 0 --group VectorEngineProfile --period-ms 100 \
    --duration 60 --out /data/run.csv

docker run --rm -v "$PWD:/data" --entrypoint /app/xmx-summary.py xmxmon /data/run.csv
```

Only `--device /dev/dri` is needed — no privileged mode, no host PID namespace,
no changes to the container running your workload.

### Leave it running

For anything beyond a one-off capture, run the daemon instead. It keeps
samplers alive across reboots and lets you start and stop captures on demand,
so you don't have to predict when to attach:

```sh
cp xmxmon.yaml.example xmxmon.yaml   # your copy; gitignored, survives git pull
mkdir -p captures
docker compose up -d                 # starts, and comes back after a reboot
```

No `docker compose`? Some distributions (including Ubuntu's `docker.io`
package) ship Docker without the Compose plugin — `docker compose version`
will say `unknown command`. Either install it (`sudo apt install
docker-compose-v2`, or Docker's own `docker-ce` packages), or skip Compose
entirely — this is the same thing in one command:

```sh
docker run -d --name xmxmon --restart unless-stopped \
    --device /dev/dri \
    -p 127.0.0.1:9143:9143 \
    -v "$PWD/xmxmon.yaml:/etc/xmxmon.yaml:ro" \
    -v "$PWD/captures:/data/captures" \
    --entrypoint /app/xmxmond.py \
    xmxmon
```

Check it, watch it live, then wrap a benchmark with a tagged capture:

```sh
curl -s localhost:9143/now                                 # is it alive?
./xmxmon-tui.py http://localhost:9143                      # live terminal view

curl -s -X POST localhost:9143/capture -d '{"name":"my-bench","device":0}'
./run-my-benchmark.sh
curl -s -X POST localhost:9143/capture/stop -d '{"device":0}'
curl -s localhost:9143/captures                            # where the file landed
```

Between captures it idles at a coarse sampling period, so leaving it up costs
very little. Edit your `xmxmon.yaml` to change devices, periods, or to enable the
web UI and Prometheus exporter — see [Configuration](#configuration) and
[Daemon mode](#daemon-mode) below, and restart the container to pick up changes.
Stop it with `docker compose down` (or `docker rm -f xmxmon` if you started it
with `docker run`).

Note that while the daemon is running it owns the device's performance
counters, so stop it before using the one-shot CLI on that same GPU.

## Quick start (host)

```sh
g++ -O2 -o xmxmon xmxmon.cpp -lze_loader

./xmxmon --list                                        # devices + metric groups
./xmxmon --device 0 --group VectorEngineProfile --list-metrics   # + descriptions
./xmxmon --device 0 --group VectorEngineProfile --period-ms 100 --out run.csv
# Ctrl-C when done — rows are written as they arrive, nothing is lost.
./xmx-summary.py run.csv
```

## Reading the output

`xmx-summary.py` answers the first question directly:

Real capture of a PyTorch XPU fp16 4096³ matmul loop, sampled from outside the
container running it:

```
460 reports total, 411 with GPU_BUSY > 1% (stats below are over busy reports only)

--- XMX (matrix engine) ---
XVE_INST_EXECUTED_XMX_INT2           total            0  avg          0  peak          0
XVE_INST_EXECUTED_XMX_INT4           total            0  avg          0  peak          0
XVE_INST_EXECUTED_XMX_INT8           total            0  avg          0  peak          0
XVE_INST_EXECUTED_XMX_FP16           total    5.226e+12  avg  1.272e+10  peak   1.63e+10  <-- ACTIVE
XVE_INST_EXECUTED_XMX_BF16           total            0  avg          0  peak          0

VERDICT: XMX IS being used (see precisions above)

--- utilization ---
GPU_BUSY                             avg        99.23  peak          100
XVE_ACTIVE                           avg        50.27  peak        50.91
...
```

Two things worth knowing before you interpret a capture:

**Sample a representative workload.** Matrix engines light up on matrix-shaped work.
An LLM decoding one token at a time is doing matrix-vector products, and XMX will
read as legitimately idle — that says nothing about whether the backend uses XMX for
batched work. Capture a batch/prefill-heavy phase before concluding anything.

**Mixed-precision instructions are counted under their wider operand.** A DPAS with,
say, 8-bit activations against 2-bit weights increments the INT8 counter, not INT2.
So `XMX_INT2` staying at zero does not mean 2-bit data never reached the matrix
engine — reaching that counter requires *both* operands to be 2-bit.

## Daemon mode

`xmxmond.py` keeps samplers running continuously and adds an HTTP API, so
benchmark scripts can turn high-rate capture on and off around a run
(`docker compose up -d`, as in [Leave it running](#leave-it-running) above).

Published on `127.0.0.1:9143` only. Always available:

| Endpoint | Purpose |
|---|---|
| `GET /now` | latest aggregate snapshot (JSON) |
| `POST /capture` | `{"name":"run1","device":0,"duration_s":600}` — switch to high-rate sampling, tee to a tagged ndjson in `captures/`, auto-revert |
| `POST /capture/stop` | `{"device":0}` — end early |
| `GET /captures` | running and finished captures |

Omit `device` on either capture call to act on every configured device at once.
A capture ends when its `duration_s` elapses or you stop it, whichever comes
first; the sampler then drops back to the idle period on its own.

### Terminal UI

```sh
./xmxmon-tui.py http://localhost:9143
```

Live bars for XMX per precision, utilization, and bandwidth, with peak-hold markers.
It's a plain HTTP client, so it works fine over ssh and doesn't need GPU access
itself. `q` quits.

### Optional: web UI and Prometheus

Both are **off by default**. Enable in `xmxmon.yaml`:

```yaml
wui: true          # live charts at GET /
prometheus: true   # GET /metrics for scraping
```

Neither endpoint has authentication, so leave the bind address on loopback unless
you've put something in front of it.

With `prometheus: true`, scrape it:

```yaml
- job_name: xmxmon
  scrape_interval: 5s
  static_configs:
    - targets: ['127.0.0.1:9143']
```

Exported series: `xmxmon_rate_per_s{device,metric}` for counters (already converted
to per-second), `xmxmon_gauge{device,metric}` for levels, and `xmxmon_capturing{device}`.
`grafana-dashboard.json` imports as a starting dashboard — it uses a datasource
picker, so choose your Prometheus instance during import.

## Configuration

Configuration lives in two files you own, both created by copying a tracked
template and both gitignored — so your settings survive `git pull` and never end
up in a commit:

```sh
cp xmxmon.yaml.example xmxmon.yaml
cp docker-compose.override.yml.example docker-compose.override.yml   # optional
```

`xmxmon.yaml` holds daemon settings: devices, metric group, sampling periods,
bind address, capture directory, and the two feature flags. See
`xmxmon.yaml.example` for the annotated list. On the host, point at any copy with
`XMXMOND_CONFIG=/path/to.yaml`.

`docker-compose.override.yml` holds machine-specific deployment changes — port
exposure, device pinning, capture volume. Compose merges it automatically when
present, so `docker compose up -d` needs no extra arguments. Leave it out
entirely and the tracked defaults apply.

Never edit `docker-compose.yml` or the `.example` files directly; upstream owns
those, and local edits there are exactly what `git pull` will fight with.

## Limits

- **One metric group per device at a time.** That's a hardware constraint, not a
  tool limitation. Sampling `MemoryProfile` or `VectorEngineStalls` means a separate
  run.
- **One streamer per device.** While the daemon is running it owns the device's
  counters; stop it before running the standalone CLI against the same GPU.
- **Counters are device-wide.** Two workloads sharing a GPU are summed together.
  Pin the workload under test to its own device.
- `VectorEngineProfile` is not available on integrated GPUs — check `--list`.

## Checking your driver stack

```sh
docker run --rm --entrypoint /app/check-driver-updates.sh xmxmon
```

Read-only: prints installed versions of the Intel GPU userspace packages and whether
newer ones are available. Exits non-zero if updates exist. Installs nothing.

## License

Apache-2.0. See [LICENSE](LICENSE).
