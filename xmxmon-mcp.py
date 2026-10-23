#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""xmxmon MCP server: agent-facing profiling over stdio JSON-RPC 2.0.

Topology: agent <--stdio--> xmxmon-mcp.py <--HTTP--> xmxmond (owns the Level
Zero streamers) and <--sqlite--> the capture store (xmxdb). This process
never touches Level Zero itself; the daemon's own validations (unknown
device, group not on device, group switch during capture) are the safety
boundary and are passed through, not reimplemented.

Protocol notes:
- stdio framing is newline-delimited JSON-RPC (NOT LSP Content-Length).
- stdout is the protocol channel: NOTHING else may write to it; diagnostics
  go to stderr only.
- run_profile jobs are unbounded: the workload runs until it exits. Result
  delivery is by handle because no MCP host holds a request open forever.
- Shoulders (idle baseline around the workload) are injected automatically
  and are operator-level (--shoulder-s), never model-facing.
"""
import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xmxdb
import xmxderive

PROTOCOL_VERSION = "2025-06-18"
KNOWN_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}

CFG = {"daemon": "http://127.0.0.1:9143", "db": "./xmxmon.db",
       "captures": "./captures", "logs": None, "shoulder_s": 2.0}
JOBS = {}          # job_id -> {state, ...}; in-memory by design
_JOBS_LOCK = threading.Lock()
_NAME_OK = re.compile(r"[^A-Za-z0-9_.-]")


def log(*a):
    print("[xmxmon-mcp]", *a, file=sys.stderr, flush=True)


# -- daemon HTTP -------------------------------------------------------------

def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(CFG["daemon"] + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:  # daemon is the boundary: pass through
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": f"http {e.code}"}


def _get(path):
    return _req("GET", path)[1]


def _post(path, body):
    return _req("POST", path, body)


# -- jobs ---------------------------------------------------------------
# The manual capture/group primitives used to be tools. They are not part of
# the agent surface anymore: any cross-call sequencing (switch -> poll ->
# capture -> stop) now lives inside run_profile, where it can't be
# mis-ordered or leaked. The daemon endpoints remain for the WUI/TUI and
# operator use.



def _switch_and_wait(device, group, timeout_s=15):
    """Switch a device's metric group and wait until it is EFFECTIVE
    (read-your-writes: /now only reports the new group after the sampler
    respawn). The daemon's own validation is the boundary; refusals raise."""
    status, body = _post("/group", {"device": device, "group": group})
    if status != 200 or not body.get("ok"):
        raise RuntimeError(f"group switch refused: {body}")
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        now = _get("/now")
        if now.get(str(device), {}).get("group") == group:
            return
        time.sleep(0.5)
    raise RuntimeError(f"group switch to {group} not effective"
                       f" within {timeout_s}s")


def _job_run(job):
    jid, name, device = job["job_id"], job["name"], job["device"]
    restore = None

    def _restore_group():
        nonlocal restore
        if restore:
            try:
                _switch_and_wait(device, restore)
                job["group_restored"] = True
            except Exception as e:
                log(f"job {jid}: group restore to {restore} failed: {e!r}")
                job["group_restored"] = False
            restore = None

    try:
        if job.get("group"):
            cur = _get("/now").get(str(device), {}).get("group")
            if cur and cur != job["group"]:
                restore = cur
                _switch_and_wait(device, job["group"])
        body = {"name": name}
        if device is not None:
            body["device"] = device
        status, started = _post("/capture", body)
        paths = started.get("started", {})
        # device=None captured all; our ndjson basename is per device
        cap_path = paths.get(str(device)) if device is not None else None
        if cap_path is None and paths:
            key = sorted(paths)[0]
            cap_path, device = paths[key], int(key)
            job["device"] = device
        if not isinstance(cap_path, str) or not cap_path.endswith(".ndjson"):
            raise RuntimeError(f"capture not started: {started}")
        job["capture_file"] = os.path.basename(cap_path)

        time.sleep(CFG["shoulder_s"])  # invisible baseline shoulder
        # Log capture is best-effort: an unwritable log dir (e.g. the
        # daemon's root-owned captures mount) must never fail the job.
        log_dir = CFG["logs"] or CFG["captures"]
        try:
            out_log = open(os.path.join(log_dir, name + ".stdout.log"), "wb")
            err_log = open(os.path.join(log_dir, name + ".stderr.log"), "wb")
            job["log_dir"] = log_dir
        except OSError as e:
            out_log = err_log = subprocess.DEVNULL
            job["log_dir"] = None
            job["logs_note"] = f"logs unavailable ({e}); output discarded"
        env = dict(os.environ)
        env.update(job.get("env") or {})
        proc = subprocess.Popen(job["argv"], stdout=out_log,
                                stderr=err_log, env=env,
                                cwd=job.get("cwd") or None,
                                start_new_session=True)
        job["pid"] = proc.pid
        t0 = time.time()
        rc = proc.wait()  # unbounded: the job takes however long it takes
        wall_s = time.time() - t0
        out_log.close()
        err_log.close()
        time.sleep(CFG["shoulder_s"])  # tail shoulder

        body = {}
        if device is not None:
            body["device"] = device
        _post("/capture/stop", body)
        _restore_group()  # group switch is device-wide; leave as found

        con = xmxdb.connect(CFG["db"])  # one connection per thread
        try:
            res = xmxdb.ingest_file(
                con, os.path.join(CFG["captures"], job["capture_file"]))
            job["run_id"] = res[0] if res else next(
                (r["run_id"] for r in xmxdb.list_runs(con)
                 if r["name"] == name), None)
        finally:
            con.close()

        def tail(p, n=20):
            try:
                with open(p, errors="replace") as f:
                    return "".join(f.readlines()[-n:])
            except OSError:
                return ""

        clogs = (os.path.join(job["log_dir"], name)
                 if job.get("log_dir") else None)
        job["result"] = {
            "job_id": jid, "name": name, "device": device,
            "exit_code": rc, "wall_s": round(wall_s, 3),
            "stdout_log": (clogs + ".stdout.log") if clogs else None,
            "stderr_log": (clogs + ".stderr.log") if clogs else None,
            "stdout_tail": tail(clogs + ".stdout.log") if clogs else "",
            "stderr_tail": tail(clogs + ".stderr.log") if clogs else "",
            "logs_note": job.get("logs_note"),
            "group": job.get("group"),
            "group_restored": job.get("group_restored"),
            "run_id": job["run_id"]}
        # Lean on purpose: the outcome, the logs, the run_id — nothing else.
        # Full summaries/dumps blew smaller context windows; the data is one
        # query away (db_schema, then query_sql).
        job["state"] = "done"
    except Exception as e:
        log(f"job {jid} failed: {e!r}")
        try:
            _post("/capture/stop", {"device": device} if device is not None
                  else {})
        except Exception:
            pass
        _restore_group()
        job["state"] = "error"
        job["error"] = repr(e)


def tool_run_profile(a):
    argv = a.get("argv")
    tool_run_profile.counter = getattr(tool_run_profile, "counter", 0)
    if not isinstance(argv, list) or not argv \
            or not all(isinstance(x, str) for x in argv):
        raise ValueError("argv must be a non-empty array of strings")
    if a.get("group") is not None and a.get("device") is None:
        raise ValueError("group requires an explicit device")
    name = _NAME_OK.sub("_", a.get("name") or os.path.basename(argv[0]))
    tool_run_profile.counter = getattr(tool_run_profile, "counter", 0)
    tool_run_profile.counter += 1
    jid = f"{name}-{int(time.time())}-{tool_run_profile.counter}"
    job = {"job_id": jid, "name": name, "argv": argv, "env": a.get("env"),
           "cwd": a.get("cwd"), "device": a.get("device"),
           "group": a.get("group"), "state": "running",
           "submitted_ts": time.time()}
    with _JOBS_LOCK:
        JOBS[jid] = job
    threading.Thread(target=_job_run, args=(job,), daemon=True).start()
    return {"job_id": jid, "state": "running"}


def tool_job_status(a):
    with _JOBS_LOCK:
        job = JOBS.get(a.get("job_id"))
    if not job:
        raise KeyError(f"no such job: {a.get('job_id')}")
    if job["state"] == "done":
        return job["result"]
    if job["state"] == "error":
        return {"job_id": job["job_id"], "state": "error",
                "error": job.get("error")}
    out = {"job_id": job["job_id"], "state": "running",
           "elapsed_s": round(time.time() - job["submitted_ts"], 1)}
    try:  # best-effort live row count from the daemon
        for c in _get("/captures").get("running", []):
            if c.get("name") == job["name"]:
                out["rows_so_far"] = c.get("rows")
    except Exception:
        pass
    return out


# -- tool dispatch -----------------------------------------------------------

def tool_db_schema(a):
    con = xmxdb.connect(CFG["db"])
    try:
        tables = []
        for g in con.execute("SELECT metric_group, table_name FROM groups"):
            group = g["metric_group"]
            cols = [{"name": c,
                     "kind": "level" if xmxderive.is_percent(c)
                             else "counter"}
                    for c in xmxdb.metric_columns(con, group)]
            n = con.execute(
                f"SELECT COUNT(*) c FROM {g['table_name']}").fetchone()["c"]
            tables.append({"table": g["table_name"], "view":
                           "v_" + g["table_name"], "metric_group": group,
                           "rows": n, "metrics": cols})
        n_runs = con.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"]
        return {"runs": n_runs,
                "spine": "runs(run_id, name, device, metric_group,"
                         " period_ms, started_ts, ended_ts, rows, path)",
                "framing": "every samples table carries: run_id, seq,"
                           " t, gpu_ns, gpu_core_clocks, avg_core_mhz,"
                           " core_mhz, extra(JSON); v_ views add name and"
                           " wall_ts (unix epoch)",
                "semantics": "levels are per-window gauges; average them."
                             " counters are per-window counts; per-second"
                             " rate = SUM(x)*1e9/SUM(gpu_ns)",
                "tables": tables}
    finally:
        con.close()


def tool_query_sql(a):
    sql = a.get("sql", "")
    max_rows = int(a.get("max_rows") or 500)
    deny = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE, sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA,
            sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_REINDEX,
            sqlite3.SQLITE_ANALYZE}
    con = sqlite3.connect(f"file:{CFG['db']}?mode=ro", uri=True)
    con.set_authorizer(
        lambda op, x, y, z, w: sqlite3.SQLITE_DENY if op in deny
        else sqlite3.SQLITE_OK)
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(max_rows + 1)
        return {"columns": cols, "rows": [list(r) for r in rows[:max_rows]],
                "truncated": len(rows) > max_rows}
    finally:
        con.close()


def _tool(fn, read_only=True):
    return {"fn": fn}


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_str = {"type": "string"}
_int = {"type": "integer"}
_num = {"type": "number"}

TOOLS = {
    "list_devices": {
        "description": "List GPU devices the daemon samples: active metric"
        " group, switchable groups, sampler period, capture status. Start"
        " here before any capture or group switch.",
        "inputSchema": _obj({}),
        "handler": lambda a: _get("/now"),
    },
    "get_snapshot": {
        "description": "Current live counters for one device: per-second"
        " rates, averaged gauges (percentages), and derived metrics, as the"
        " daemon computes them. Counters are device-wide (all processes on"
        " the GPU summed). Use n>1 for a raw series, e.g. an idle baseline;"
        " no verdict is computed — read the numbers.",
        "inputSchema": _obj({"device": _int, "n": _int, "interval_s": _num},
                            ["device"]),
        "handler": None,  # custom: loops
    },
    "query_sql": {
        "description": "Read-only SQL over the run store. Tables: runs"
        " (capture spine), groups (metric group registry), and one"
        " samples_<Group> table per metric group (e.g."
        " samples_VectorEngineProfile) with views v_samples_<Group> adding"
        " wall_ts. SELECT/WITH only, single statement, enforced server-side."
        " For per-second rates: SUM(counter)*1e9/NULLIF(SUM(gpu_ns),0);"
        " average levels.",
        "inputSchema": _obj({"sql": _str, "max_rows": _int}, ["sql"]),
        "handler": tool_query_sql,
    },
    "run_profile": {
        "description": "Execute a program to profiles its selected group."
        " argv is an array (program + args); env merges over the server's"
        " environment (SYCL selectors such as ONEAPI_DEVICE_SELECTOR go"
        " here). Optional group: the metric group to sample under (e.g."
        " VectorEngineProfile, ComputeBasic, MemoryProfile,"
        " VectorEngineStalls — see list_devices' switchable list); the job"
        " switches, waits for it to take effect, and restores the previous"
        " group afterwards. The switch is DEVICE-WIDE while the job runs."
        " A capture with short idle shoulders is started and stopped"
        " automatically — invisible to the caller. The job is UNBOUNDED:"
        " returns immediately with a receipt; fetch the outcome with"
        " job_status, then explore the run with db_schema + query_sql."
        " Counters are device-wide: anything else"
        " on the GPU during the run is summed in; the idle shoulders make"
        " contamination visible. (For docker: pass env as -e flags in argv,"
        " the env arg only reaches the docker CLI.)",
        "inputSchema": _obj({"argv": {"type": "array",
                                      "items": {"type": "string"}},
                             "env": {"type": "object"}, "cwd": _str,
                             "name": _str, "device": _int,
                             "group": _str}, ["argv"]),
        "handler": tool_run_profile,
    },
    "job_status": {
        "description": "Fetch the outcome of a run_profile job. Small by"
        " design. When done: exit_code, wall_s, log paths and tails,"
        " group/group_restored (if requested), and run_id — the handle into"
        " the store. Explore the run's DATA with db_schema + query_sql,"
        " not here. While running: state, elapsed time, live row count."
        " Error: state + reason.",
        "inputSchema": _obj({"job_id": _str}, ["job_id"]),
        "handler": tool_job_status,
    },
    "db_schema": {
        "description": "Get the database schema so you can use the query_sql tool",
        "inputSchema": _obj({}),
        "handler": None,  # custom: needs connection
    },
}


def _call(name, a):
    if name == "get_snapshot":
        out = []
        n, iv = int(a.get("n") or 1), float(a.get("interval_s") or 1)
        for i in range(n):
            now = _get("/now")
            key = str(a["device"])
            if key not in now:
                raise KeyError(f"no device {a['device']}")
            out.append(now[key])
            if i < n - 1:
                time.sleep(iv)
        return {"snapshots": out}
    if name == "db_schema":
        return tool_db_schema(a)
    return TOOLS[name]["handler"](a)


# -- JSON-RPC loop -----------------------------------------------------------

def _send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _err(rid, code, message):
    _send({"jsonrpc": "2.0", "id": rid,
           "error": {"code": code, "message": message}})


def _shutdown(*_):
    with _JOBS_LOCK:
        running = [j for j in JOBS.values() if j["state"] == "running"]
    for j in running:
        try:  # the workload tree dies with us; the capture stays valid
            os.killpg(j.get("pid", 0), signal.SIGTERM)
        except Exception:
            pass
    sys.exit(0)


def handle(msg):
    rid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        v = msg.get("params", {}).get("protocolVersion")
        _send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": v if v in KNOWN_VERSIONS else PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "xmxmon-mcp", "version": "0.3.0"}}})
    elif method == "ping":
        _send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method and method.startswith("notifications/"):
        pass
    elif method == "tools/list":
        _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": n, "description": t["description"],
             "inputSchema": t["inputSchema"]} for n, t in TOOLS.items()]}})
    elif method == "tools/call":
        p = msg.get("params") or {}
        name, a = p.get("name"), p.get("arguments") or {}
        try:
            if name not in TOOLS:
                raise KeyError(f"unknown tool: {name}")
            result = _call(name, a)
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(result, default=str)}]}})
        except Exception as e:
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text":
                             json.dumps({"error": repr(e)})}],
                "isError": True}})
    elif rid is not None:
        _err(rid, -32601, f"method not found: {method}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--daemon", default=CFG["daemon"])
    ap.add_argument("--db", default=CFG["db"])
    ap.add_argument("--captures", default=CFG["captures"])
    ap.add_argument("--logs", default=None,
                    help="directory for run_profile stdout/stderr logs"
                         " (default: the captures dir; output is discarded"
                         " if not writable — never jobs)")
    ap.add_argument("--shoulder-s", type=float, default=CFG["shoulder_s"],
                    help="idle baseline around run_profile workloads"
                         " (operator-level, never model-facing)")
    args = ap.parse_args()
    CFG.update(daemon=args.daemon, db=args.db, captures=args.captures,
               logs=args.logs, shoulder_s=args.shoulder_s)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    log(f"daemon={CFG['daemon']} db={CFG['db']} captures={CFG['captures']}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except ValueError:
            _err(None, -32700, "parse error")


if __name__ == "__main__":
    main()
