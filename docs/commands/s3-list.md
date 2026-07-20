# `ajl s3 list`

The composable single-level primitive `s3 scan` is built from: one
`list-objects-v2` per seed, `CommonPrefixes` emitted as pipeable
`s3:prefix` records instead of being recursed into automatically. Same
engine as `s3 scan` (see [../scan-design.md](../scan-design.md)) with
recursion turned off. Source: `src/ajl/scan.py` (`build_list_parser`,
`run_list`).

## Flags

| Flag | Default | Notes |
|---|---|---|
| `uris` / `--uri` | — | one or more `s3://bucket/prefix`; positional and `--uri` are equivalent |
| `--delimiter` | — | group keys into `s3:prefix` records at this delimiter |
| `--start-after` | — | list keys after this one (uri seeds only) |
| `--page-size` | `1000` | `MaxKeys` per request |
| `--include-tags` | off | add a `Tags` map via one `get-object-tagging` call per object |
| `--no-progress` | off | disable the live progress line |
| `--no-fast` | off | list via boto3 instead of the raw signed-HTTP fast path |
| `--failed-out FILE` | — | write failed listings as JSONL seeds |

## The classic fan-out dance

Every `s3:prefix` record carries its own `Delimiter`, so piping straight
back into another `ajl s3 list --params-json -` repeats the grouping one
level deeper — each stage only needs to know "keep going," not which level
it's at:

```shell
ajl s3 list s3://my-bucket --delimiter / \
  | ajl s3 list --params-json - \
  | ajl s3 list --params-json - --jq 'del(.Delimiter)' \
  | ajl s3 list --params-json - --workers 32
```

`--jq 'del(.Delimiter)'` is the deliberate way to stop repeating one level
and make a stage recurse — a record with no `Delimiter` seeds a *plain*
listing (no grouping) of everything under that prefix from then on.

## Output shape

Same lean contract as `s3 scan` (see [s3-scan.md](s3-scan.md)'s Output
shape section) — `s3:object` records with `ajl.uri` carrying identity, and
`s3:prefix` records for each `CommonPrefixes` entry:

```json
{"Bucket": "my-bucket", "Prefix": "logs/", "Delimiter": "/",
 "ajl": {"type": "s3:prefix", "uri": "s3://my-bucket/logs/"}}
```

Records emitted by `ajl` pipe straight back into `--params-json -` for
either command — the trailing `ajl` object and any params the target
operation doesn't accept are dropped automatically (see
[../request-flow.md](../request-flow.md) for exactly how).

## See also

- [s3-scan.md](s3-scan.md) — the recursive version of the same engine
- [../scan-design.md](../scan-design.md) — the underlying design (applies to both commands)
