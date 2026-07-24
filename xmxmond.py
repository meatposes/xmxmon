#!/usr/bin/env python3
# Copyright 2026 the xmxmon authors
# SPDX-License-Identifier: Apache-2.0
"""xmxmond — daemon around the xmxmon sampler.

Spawns one xmxmon --json child per configured device, aggregates samples into
per-second rates/averages, and serves:

Always on:
  GET  /now            latest aggregate snapshot as JSON
  GET  /captures       list finished/running captures
  POST /capture        {"name": "...", "device": 1, "duration_s": 600,
                        "period_ms": 100}  -> start tagged high-rate capture
  POST /capture/stop   {"device": 1}       -> end capture early

Opt-in (disabled by default; enable in config):
  GET  /metrics        Prometheus text exposition   [prometheus: true]
  GET  /               web UI (live charts)         [wui: true]
  GET  /events         SSE stream feeding the web UI [wui: true]

Config: YAML file (default /etc/xmxmon.yaml, override with XMXMOND_CONFIG).
"""
import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

import xmxderive

DEFAULTS = {
    "binary": "/app/xmxmon",
    "devices": [0],
    "group": "VectorEngineProfile",
    # Optional per-device override, e.g. {1: VectorEngineStalls}. Only one
    # metric group can be active per device, so sampling stalls on one card
    # while another samples XMX is the way to see both at once.
    "groups": {},
    "idle_period_ms": 500,
    "capture_period_ms": 100,
    "read_ms": 250,
    "listen": "127.0.0.1:9143",
    "capture_dir": "/data/captures",
    "window_s": 1.0,          # aggregation window
    "prometheus": False,      # opt-in: expose GET /metrics
    "wui": False,             # opt-in: expose GET / and GET /events
}
CFG = dict(DEFAULTS)

# Levels are averaged; everything else is a per-second counter. The canonical
# percentage set lives in xmxderive so the offline summary agrees.
SKIP = {"t", "GpuTime", "GpuCoreClocks", "QueryBeginTime", "ReportReason",
        "ContextIdValid", "ContextId", "SourceId", "StreamMarker"}


class Sampler:
    """One xmxmon child per device; restartable with a different period."""

    def __init__(self, cfg, device):
        self.cfg = cfg
        self.device = device
        groups = cfg.get("groups") or {}
        # YAML keys may arrive as int or str depending on how they were written.
        self.group = groups.get(device, groups.get(str(device), cfg["group"]))
        self.proc = None
        self.window = deque()          # (wall_t, metrics dict)
        self.lock = threading.Lock()
        self.capture = None            # dict: name, file, until, rows
        self.period_ms = cfg["idle_period_ms"]
        self._want_period = self.period_ms
        threading.Thread(target=self._run, daemon=True).start()

    def _spawn(self):
        cmd = [self.cfg["binary"], "--device", str(self.device),
               "--group", self.group,
               "--period-ms", str(self._want_period),
               "--read-ms", str(self.cfg["read_ms"]), "--json"]
        self.period_ms = self._want_period
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)

    def _run(self):
        while True:
            self._spawn()
            for line in self.proc.stdout:
                if not line.startswith("{"):
                    continue
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                now = time.time()
                with self.lock:
                    self.window.append((now, m))
                    cutoff = now - self.cfg["window_s"]
                    while self.window and self.window[0][0] < cutoff:
                        self.window.popleft()
                    cap = self.capture
                    if cap:
                        cap["file"].write(json.dumps(m) + "\n")
                        cap["rows"] += 1
                        if cap["until"] and now >= cap["until"]:
                            self._end_capture_locked()
                if self._want_period != self.period_ms:
                    break  # restart child with new period
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                pass
            time.sleep(0.5)

    def snapshot(self):
        with self.lock:
            rows = [m for _, m in self.window]
            cap = self.capture and {"name": self.capture["name"],
                                    "rows": self.capture["rows"]}
        if not rows:
            return {"device": self.device, "n": 0, "capture": cap,
                    "group": self.group}
        secs = self.cfg["window_s"]
        out = {"device": self.device, "n": len(rows), "capture": cap,
               "group": self.group, "period_ms": self.period_ms,
               "rates": {}, "gauges": {}}
        keys = rows[-1].keys()
        for k in keys:
            if k in SKIP:
                continue
            vs = [r.get(k, 0.0) for r in rows]
            if xmxderive.is_percent(k):
                out["gauges"][k] = sum(vs) / len(vs)
            else:
                out["rates"][k] = sum(vs) / secs
        # Ratios need rates and gauges together; percentages live in gauges.
        merged = dict(out["rates"])
        merged.update(out["gauges"])
        out["derived"] = [
            {"label": lbl, "value": (None if val != val or val in
                                     (float("inf"), float("-inf")) else val),
             "unit": unit, "note": note}
            for lbl, val, unit, note in xmxderive.derive(merged)
        ]
        return out

    # -- capture control ---------------------------------------------------
    def start_capture(self, name, duration_s, period_ms):
        os.makedirs(self.cfg["capture_dir"], exist_ok=True)
        path = os.path.join(self.cfg["capture_dir"],
                            f"{name}-dev{self.device}-{int(time.time())}.ndjson")
        with self.lock:
            if self.capture:
                return None
            self.capture = {"name": name, "path": path, "rows": 0,
                            "file": open(path, "w"), "start": time.time(),
                            "until": time.time() + duration_s if duration_s else None}
        self._want_period = period_ms or self.cfg["capture_period_ms"]
        return path

    def _end_capture_locked(self):
        cap = self.capture
        cap["file"].close()
        HISTORY.append({"name": cap["name"], "path": cap["path"],
                        "device": self.device, "rows": cap["rows"],
                        "duration_s": round(time.time() - cap["start"], 1)})
        self.capture = None
        self._want_period = self.cfg["idle_period_ms"]

    def stop_capture(self):
        with self.lock:
            if not self.capture:
                return False
            self._end_capture_locked()
        return True


HISTORY = []
SAMPLERS = {}
SSE_CLIENTS = set()


def broadcaster():
    while True:
        time.sleep(0.5)
        if not SSE_CLIENTS:
            continue
        snap = json.dumps({str(d): s.snapshot() for d, s in SAMPLERS.items()})
        dead = []
        for q in list(SSE_CLIENTS):
            try:
                q.put_nowait(snap)
            except queue.Full:
                dead.append(q)
        for q in dead:
            SSE_CLIENTS.discard(q)


def _slug(label):
    """Stable Prometheus-safe metric name from a derived label."""
    s = label.lower().replace("/", " per ").replace("%", "pct")
    s = "".join(c if c.isalnum() else "_" for c in s)
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_")


def prometheus_text():
    lines = []
    for d, s in SAMPLERS.items():
        snap = s.snapshot()
        for k, v in snap.get("gauges", {}).items():
            lines.append(f'xmxmon_gauge{{device="{d}",metric="{k}"}} {v:.6g}')
        for k, v in snap.get("rates", {}).items():
            lines.append(f'xmxmon_rate_per_s{{device="{d}",metric="{k}"}} {v:.6g}')
        # Derived ratios, so Grafana graphs exactly what the TUI/summary show.
        for item in snap.get("derived", []):
            val = item.get("value")
            if val is None:
                continue
            lines.append(f'xmxmon_derived{{device="{d}",'
                         f'metric="{_slug(item["label"])}"}} {val:.6g}')
        lines.append(f'xmxmon_capturing{{device="{d}"}} '
                     f'{1 if snap.get("capture") else 0}')
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/events") and not CFG["wui"]:
            return self._send(404, '{"error":"web UI disabled (set wui: true)"}')
        if self.path == "/metrics" and not CFG["prometheus"]:
            return self._send(404,
                              '{"error":"exporter disabled (set prometheus: true)"}')
        if self.path == "/":
            self._send(200, WUI_HTML, "text/html")
        elif self.path == "/now":
            self._send(200, json.dumps(
                {str(d): s.snapshot() for d, s in SAMPLERS.items()}))
        elif self.path == "/metrics":
            self._send(200, prometheus_text(), "text/plain; version=0.0.4")
        elif self.path == "/captures":
            running = [{**{"device": d}, **s.snapshot()["capture"]}
                       for d, s in SAMPLERS.items() if s.snapshot()["capture"]]
            self._send(200, json.dumps({"running": running, "done": HISTORY}))
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = queue.Queue(maxsize=8)
            SSE_CLIENTS.add(q)
            try:
                while True:
                    msg = q.get()
                    self.wfile.write(f"data: {msg}\n\n".encode())
                    self.wfile.flush()
            except Exception:
                SSE_CLIENTS.discard(q)
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, '{"error":"bad json"}')
        if self.path == "/capture":
            dev = body.get("device")
            targets = [dev] if dev is not None else list(SAMPLERS)
            started = {}
            for d in targets:
                if d not in SAMPLERS:
                    return self._send(404, f'{{"error":"no device {d}"}}')
                p = SAMPLERS[d].start_capture(
                    body.get("name", "capture"), body.get("duration_s"),
                    body.get("period_ms"))
                started[d] = p or "already capturing"
            self._send(200, json.dumps({"started": started}))
        elif self.path == "/capture/stop":
            dev = body.get("device")
            targets = [dev] if dev is not None else list(SAMPLERS)
            self._send(200, json.dumps(
                {d: SAMPLERS[d].stop_capture() for d in targets if d in SAMPLERS}))
        else:
            self._send(404, '{"error":"not found"}')


def main():
    cfg = CFG
    path = os.environ.get("XMXMOND_CONFIG", "/etc/xmxmon.yaml")
    # A missing bind-mount source makes Docker create a *directory* at the
    # mount point, so isfile() is the real test. FALLBACK is baked into the
    # image: without it, an unconfigured container would bind the built-in
    # 127.0.0.1 default and be unreachable through its own port mapping.
    FALLBACK = "/app/xmxmon.yaml.default"
    if not os.path.isfile(path) and os.path.isfile(FALLBACK):
        print(f"NOTE: no config at {path}; using image defaults. "
              f"Copy xmxmon.yaml.example to xmxmon.yaml to configure.")
        path = FALLBACK
    if os.path.isfile(path):
        try:
            cfg.update(yaml.safe_load(open(path)) or {})
        except (OSError, yaml.YAMLError) as e:
            print(f"WARNING: could not read {path} ({e}); using built-in defaults")
    else:
        print(f"WARNING: no config file at {path}; using built-in defaults")
    os.environ.setdefault("ZET_ENABLE_METRICS", "1")
    for d in cfg["devices"]:
        SAMPLERS[d] = Sampler(cfg, d)
    threading.Thread(target=broadcaster, daemon=True).start()
    host, port = cfg["listen"].rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), Handler)
    print(f"xmxmond listening on {cfg['listen']}, devices {cfg['devices']}, "
          f"group {cfg['group']}, "
          f"prometheus={'on' if cfg['prometheus'] else 'off'}, "
          f"wui={'on' if cfg['wui'] else 'off'}")
    signal.signal(signal.SIGTERM, lambda *a: os._exit(0))
    srv.serve_forever()


WUI_HTML = ""  # populated below from wui.html at container build; see loader
try:
    WUI_HTML = open(os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "wui.html")).read()
except OSError:
    WUI_HTML = "<h1>xmxmond</h1><p>wui.html missing; API endpoints still work.</p>"

if __name__ == "__main__":
    main()
