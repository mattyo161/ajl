# Analyzing an inventory run with DuckDB

`tools/build-duckdb.py` + `tools/security-checks.sql` (see
[tools/README.md](../tools/README.md) for the quick-start) turn a
`tools/inventory.sh` run into something you can actually query. This doc
covers the workflow end to end and the gotchas that aren't obvious from the
table names alone.

## Workflow

```shell
# 1. Run the inventory (add --api-log if you want call-level timing/retry
#    data available too — see "Analyzing apilog" below)
AJL_MODELS_DIR=src/ajl/models AJL_APILOG=1 uv run bash tools/inventory.sh

# 2. Build the DuckDB file
uv run tools/build-duckdb.py

# 3. Query it
duckdb .temp/inventory.duckdb
duckdb .temp/inventory.duckdb < tools/security-checks.sql
```

Re-running `build-duckdb.py` is safe and idempotent — every table is
`CREATE OR REPLACE`d from the current `.temp/data/*.jsonl` contents, so
re-running `inventory.sh` and then `build-duckdb.py` again gives you a
fresh snapshot to compare against a previous one (e.g. copy the `.duckdb`
file aside first if you want to diff before/after a remediation).

## Table shape

One table per non-empty `<service>-<resource>.jsonl` file, named after the
file (hyphens become underscores: `ec2-security-groups.jsonl` →
`ec2_security_groups`). Every ajl-shaped record has a trailing `ajl` STRUCT
column:

```sql
DESCRIBE ec2_security_groups;
-- ...
-- ajl   STRUCT("type" VARCHAR, id VARCHAR, "name" VARCHAR, arn VARCHAR,
--              tags MAP(VARCHAR, VARCHAR),
--              stamp STRUCT(profile VARCHAR, region VARCHAR, account VARCHAR))
```

Access nested fields with dot notation: `ajl.name`, `ajl.stamp.account`.
Per-bucket S3 config tables (`s3_bucket_versioning`,
`s3_bucket_lifecycle_configuration`, ...) carry the bucket name in
`ajl.stamp.Bucket` instead of `ajl.name` (those API responses never echo
the bucket back) — that's the join key back to `s3_buckets`.

`s3 scan`/`s3 list`'s lean records only carry `ajl.type`/`ajl.uri`, no
`id`/`name`/`arn`/`tags` — they're not part of `tools/inventory.sh` today,
but if you load one of their output files the same way, expect a narrower
`ajl` struct.

### Gotchas

- **Unnesting a list-of-structs column two levels deep needs
  `list_transform`+`flatten`, not a double `unnest` in one `FROM`.**
  `ec2_security_groups.IpPermissions` is `STRUCT(..., IpRanges
  STRUCT(CidrIp VARCHAR, ...)[])[]` — a list of permission-structs, each
  holding its own list of CIDR-structs. `unnest(sg.IpPermissions) AS p,
  unnest(p.IpRanges) AS rg` in the same `FROM` clause does **not** work in
  DuckDB (`Binder Error: Referenced table "p" not found`). Flatten first:
  ```sql
  SELECT 1 FROM unnest(flatten(list_transform(sg.IpPermissions, p -> p.IpRanges))) AS t(rg)
  WHERE t.rg.CidrIp = '0.0.0.0/0'
  ```
- **DuckDB lowercases some struct field names on read even when the source
  JSON was PascalCase.** `AccessKeyLastUsed.LastUsedDate` sometimes reads
  back as `.lastuseddate` depending on access path. If a struct-field read
  comes back unexpectedly `NULL`, try the all-lowercase form before
  assuming the data's missing.
- **`fetchdf()` needs pandas/numpy, which aren't part of this script's
  declared dependencies.** Use `fetchall()` (or run via the `duckdb` CLI
  binary directly) unless you've separately installed them.
- **A missing table means that resource type had zero results (or that
  inventory.sh section hasn't been curated/run yet), not an error** —
  `build-duckdb.py` skips zero-byte files rather than creating an empty
  table for them.

## Analyzing apilog (call-level timing, retries, errors)

`apilog` is loaded whole, not scoped to one run — it accumulates across
every invocation ever made with `--api-log`/`AJL_APILOG=1`, going back as
far as the file hasn't been rotated/deleted. Isolate one run by its own
timestamp range:

```sql
-- find the window (or just note it from `time` around your inventory.sh run)
SELECT min(Ts), max(Ts) FROM apilog;

-- per-service/operation summary for that window
SELECT Service, Operation,
       count(*) AS calls,
       round(sum(DurationS), 1) AS total_duration_s,
       sum(Attempts) AS total_attempts,
       sum(Attempts) - count(*) AS retries,
       sum(CASE WHEN Outcome != 'success' THEN 1 ELSE 0 END) AS errors
FROM apilog
WHERE Ts BETWEEN '2026-07-20T03:05:56' AND '2026-07-20T03:27:48'
GROUP BY 1, 2
ORDER BY total_duration_s DESC;
```

`Attempts` is the botocore retry count (1 = succeeded first try); sum
`Attempts - 1` per call (or `sum(Attempts) - count(*)` aggregated) for
total retries, not `Attempts` alone.

### Checking whether `inventory.sh` sections actually overlap

Because `inventory.sh` runs one `ajl <service> <op>` invocation after
another with no cross-command concurrency, each service's calls form a
contiguous, non-overlapping time window inside one run. This is a cheap way
to confirm (or catch a regression in) that sequencing:

```sql
WITH windows AS (
    SELECT Service, min(Ts) AS starts, max(Ts) AS ends
    FROM apilog
    WHERE Ts BETWEEN '2026-07-20T03:05:56' AND '2026-07-20T03:27:48'
    GROUP BY Service
)
SELECT Service, starts, ends,
       starts < lag(ends) OVER (ORDER BY starts) AS overlaps_previous
FROM windows
ORDER BY starts;
```

A `true` in `overlaps_previous` for anything other than `sts` (which fires
opportunistically throughout for `--stamp-session` account resolution)
means two sections ran concurrently — either a genuine change to how the
script is invoked, or two background runs happened to overlap.

## Security-posture monitoring queries

See [`tools/security-checks.sql`](../tools/security-checks.sql) — one query
block per finding category, each with a comment explaining what an empty
vs. non-empty result means. They're written generically (no hardcoded
account IDs or customer names) so the file is safe to keep in a public repo
and reuse across accounts or re-run after remediation to check progress.
