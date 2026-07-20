#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["duckdb>=1.0"]
# ///
"""Load a `tools/inventory.sh` run (plus, optionally, `--api-log`'s telemetry)
into a DuckDB file for ad-hoc analysis -- security review, performance
review, or just poking at what an account actually looks like.

Not part of the `ajl` package itself (nothing here ships in the wheel, and
`duckdb` is not a runtime dependency of ajl) -- this is a standalone
analysis tool, run directly via its PEP 723 script header:

    uv run tools/build-duckdb.py
    uv run tools/build-duckdb.py --data-dir .temp/data --out .temp/inventory.duckdb
    uv run tools/build-duckdb.py --no-apilog        # skip the apilog table

One table per non-empty `<data-dir>/<service>-<resource>.jsonl` file,
named after the file (hyphens -> underscores). Every ajl-shaped record has
a trailing `ajl` STRUCT column (`ajl.type`/`.id`/`.name`/`.arn`/`.tags`/
`.uri`/`.stamp`); `s3 scan`/`s3 list`'s lean records only carry `ajl.type`/
`.uri`. `read_json_auto(..., union_by_name=true, ignore_errors=true)`
handles files where records don't all share one schema (optional fields
present on some, absent on others) and skips any malformed line rather
than failing the whole load -- see docs/duckdb-analysis.md for query
patterns and gotchas (struct field casing, CloudTrail's JSON-as-string
columns, etc).

The apilog table (`~/.local/state/ajl/apilog.jsonl` by default, or
`AJL_APILOG_FILE`) accumulates across every run ajl has ever made with
`--api-log`/`AJL_APILOG=1` on -- it is loaded in full, not scoped to one
run, so a query needs its own `WHERE Ts BETWEEN ...` to isolate a
particular inventory run. See `tools/security-checks.sql` for a starting
set of monitoring queries against the inventory tables this produces.
"""

import argparse
import os
import sys
from pathlib import Path

import duckdb


def default_apilog_file():
    return os.environ.get("AJL_APILOG_FILE") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
        "ajl",
        "apilog.jsonl",
    )


def table_name(path):
    return path.stem.replace("-", "_")


def load_jsonl_table(con, name, path):
    con.execute(
        "CREATE OR REPLACE TABLE {} AS "
        "SELECT * FROM read_json_auto(?, format='newline_delimited', "
        "union_by_name=true, ignore_errors=true)".format(name),
        [str(path)],
    )
    return con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", default=".temp/data",
                         help="directory of <service>-<resource>.jsonl files (default: .temp/data)")
    parser.add_argument("--out", default=".temp/inventory.duckdb",
                         help="DuckDB file to write (default: .temp/inventory.duckdb)")
    parser.add_argument("--apilog-file", default=None,
                         help="apilog JSONL path (default: $AJL_APILOG_FILE or "
                              "~/.local/state/ajl/apilog.jsonl)")
    parser.add_argument("--no-apilog", action="store_true",
                         help="skip loading the apilog table")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"build-duckdb: no such directory: {data_dir}", file=sys.stderr)
        return 1

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(out_path))

    files = sorted(data_dir.glob("*.jsonl"))
    loaded, skipped_empty, failed = [], [], []
    for path in files:
        if path.stat().st_size == 0:
            skipped_empty.append(path.name)
            continue
        name = table_name(path)
        try:
            rows = load_jsonl_table(con, name, path)
        except Exception as exc:
            failed.append((path.name, str(exc)))
            continue
        loaded.append((name, rows))

    apilog_rows = None
    if not args.no_apilog:
        apilog_path = Path(args.apilog_file or default_apilog_file())
        if apilog_path.is_file() and apilog_path.stat().st_size > 0:
            apilog_rows = load_jsonl_table(con, "apilog", apilog_path)
        else:
            print(f"build-duckdb: no apilog file at {apilog_path}, skipping "
                  f"(pass --no-apilog to silence this)", file=sys.stderr)

    con.close()

    print(f"build-duckdb: wrote {out_path}")
    print(f"  {len(loaded)} table(s) loaded, {sum(r for _, r in loaded)} rows total")
    print(f"  {len(skipped_empty)} empty file(s) skipped (no resources of that type)")
    if apilog_rows is not None:
        print(f"  apilog: {apilog_rows} rows (spans every --api-log run to date, "
              f"not just this one -- filter by Ts to isolate a run)")
    if failed:
        print(f"  {len(failed)} file(s) failed to load:", file=sys.stderr)
        for name, err in failed:
            print(f"    {name}: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
