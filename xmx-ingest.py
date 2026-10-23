#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Load xmxmon capture files (.ndjson) into the SQLite run store (xmxdb).

Idempotent: a file whose path is already in `runs` is skipped, so re-running
over a captures directory is the normal catch-up path. The store is fully
rebuildable — delete the db and re-run this to reconstruct from captures.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xmxdb


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", default=["./captures"],
                    help="capture files and/or directories (default:"
                         " ./captures)")
    ap.add_argument("--db", default="./xmxmon.db", help="SQLite db path")
    args = ap.parse_args()

    files = []
    for p in args.paths:
        if os.path.isdir(p):
            files += [os.path.join(p, f) for f in sorted(os.listdir(p))
                      if f.endswith(".ndjson")]
        else:
            files.append(p)

    con = xmxdb.connect(args.db)
    added = skipped = 0
    for f in files:
        res = xmxdb.ingest_file(con, f)
        if res is None:
            skipped += 1
        else:
            run_id, group = res
            added += 1
            print(f"ingested {os.path.basename(f)} -> run {run_id} [{group}]")
    print(f"{added} ingested, {skipped} already present/empty"
          f" ({len(xmxdb.list_runs(con))} runs total in {args.db})")


if __name__ == "__main__":
    main()
