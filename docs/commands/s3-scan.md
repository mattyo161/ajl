# `ajl s3 scan`

Recursive bucket/prefix inventory, orders of magnitude faster than
`list-objects-v2 --recursive`-style serial listing, via a delimiter
fan-out worker pool and adaptive range splitting. This doc is the practical
reference (flags, output shape, examples); for *why* it's built this way —
the keyspace-partitioning engine, the splitter contract, real benchmark
numbers — see [../scan-design.md](../scan-design.md). Source:
`src/ajl/scan.py` (`build_scan_parser`, `run_scan`).

## Flags

| Flag | Default | Notes |
|---|---|---|
| `uris` / `--uri` | — | one or more `s3://bucket/prefix`; positional and `--uri` are equivalent, mix freely |
| `--delimiters` | `""` | schedule, one level per entry, e.g. `'/ / .'` — see scan-design.md |
| `--split` | `radix` | range splitter for hot prefixes once the schedule is exhausted: `radix`, `hex2`, `hex3`, `alnum2`, `b64-2` |
| `--split-class` | — | `pkg.mod:ClassName` drop-in splitter, overrides `--split` |
| `--split-after` | `10` | pages a task lists serially before asking the splitter to fan it out |
| `--max-fan` | `2000` | max child prefixes from one delimiter listing before falling back to range splitting |
| `--page-size` | `1000` | `MaxKeys` per request |
| `--emit-prefixes` | off | also emit `s3:prefix` records for discovered prefixes (scan's own traversal doesn't need these; turn it on to see the structure) |
| `--include-tags` | off | add a `Tags` map via one `get-object-tagging` call per object — expensive at volume, off by default |
| `--no-progress` | off | disable the live progress line (auto-off when stderr isn't a terminal) |
| `--no-fast` | off | list via boto3 instead of the raw signed-HTTP fast path — escape hatch for exotic setups |
| `--failed-out FILE` | — | write failed tasks as re-runnable `--params-json` seed lines |

Plus the [global flags](../../README.md) (`--workers`, `--cache`, `--profile`/`--region`, ...).

## Output shape

Deliberately lean — see scan-design.md's "Records are lean by design":

```json
{"Type": "s3:object", "Uri": "s3://my-bucket/logs/2026/07/17/x.log.gz",
 "Bucket": "my-bucket", "Key": "logs/2026/07/17/x.log.gz",
 "LastModified": "...", "ETag": "\"...\"", "Size": 8421, "StorageClass": "STANDARD"}
```

No `Id`/`Name`/`Arn` — `Uri` carries identity, and at inventory volumes
those would just repeat it. `Tags` only appears with `--include-tags`. The
raw `list-objects-v2` `Contents` item's own fields (`Key`, `LastModified`,
`ETag`, `Size`, `StorageClass`, ...) are merged straight in. With
`--emit-prefixes`, discovered prefixes also emit:

```json
{"Type": "s3:prefix", "Uri": "s3://my-bucket/logs/2026/", "Bucket": "my-bucket",
 "Prefix": "logs/2026/", "Delimiter": "/"}
```

## Resuming a failed run

`--failed-out failed.jsonl` writes one seed line per failed task (bucket,
prefix, delimiters remaining, and — critically — `StartAfter` advanced to
the last key that task actually emitted), so a re-run only re-lists what's
missing:

```shell
ajl s3 scan s3://my-bucket --delimiters '/ / /' --workers 64 --failed-out failed.jsonl
ajl s3 scan --params-json failed.jsonl --workers 64   # re-run only the misses
```

## `--learn`/`--api-log` integration

A scan's `--learn` record includes the run's `Stats` (tasks, objects,
prefixes, calls, failures) and up to 12 sample `Slices` — the actual
prefix/range jumps the splitter made, never the per-page marker churn (see
[../data-files.md](../data-files.md)).

## Examples

```shell
ajl s3 scan s3://my-bucket --delimiters '/ / /' --workers 64 --failed-out failed.jsonl
ajl s3 scan s3://my-bucket/tmp/ \
  | parallel --pipe -N1000 "jq -sc '{Delete: {Objects: map({Key: .Id}), Quiet: true}}'" \
  | ajl s3 delete-objects --bucket my-bucket --params-json -
```

## See also

- [../scan-design.md](../scan-design.md) — the engine's design: partitioning invariant, splitter contract, benchmarks
- [s3-list.md](s3-list.md) — the composable single-level primitive `scan` is built on the same engine as
