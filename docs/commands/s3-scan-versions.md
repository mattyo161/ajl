# `ajl s3 scan-versions`

Same engine, flags, and output shape as [`ajl s3 scan`](s3-scan.md), but
speaks `list-object-versions` instead of `list-objects-v2` — recursively
inventories object versions and delete markers instead of current objects
only. A separate command rather than a `--versions` flag on `scan`, so
`scan`'s existing (and heavily-optimized) code path is provably untouched.
Source: `src/ajl/scan.py` (`build_scan_versions_parser`,
`run_scan_versions`).

Delimiter fan-out and adaptive range splitting work exactly as they do for
`scan` — partitioning decisions are made on the object *key* space, which
is identical whether you're listing current objects or every version of
them, so the splitter itself needed no changes at all for this command to
exist.

## Auto-detection

Same as [`list-versions`](s3-list-versions.md#auto-detection): one
`get-bucket-versioning` call per distinct bucket among the seeds, before
listing starts, decides whether it's worth paying for
`ListObjectVersions`. A never-versioned bucket falls back to the plain
`scan` path automatically.

| Flag | Default | Notes |
|---|---|---|
| everything from [`s3 scan`](s3-scan.md) | | unchanged |
| `--force-versions` | off | skip the `get-bucket-versioning` check, always list as versions (fails to *not* versioned + a stderr warning if the check itself is denied) |

## Output shape

Same as [`list-versions`](s3-list-versions.md#output-shape) — `Versions`
entries as `s3:object-version`, `DeleteMarkers` entries as
`s3:delete-marker`, both lean (`ajl.uri` carries identity, no
`id`/`name`/`arn`). With `--emit-prefixes`, discovered prefixes still emit
as `s3:prefix`, unchanged from `scan`.

## Examples

```shell
ajl s3 scan-versions s3://my-bucket --delimiters '/ / /' --workers 64
ajl s3 scan-versions --params-json failed.jsonl --workers 64  # resume a failed run
```

## See also

- [s3-scan.md](s3-scan.md) — the plain (current-objects-only) version of this command
- [s3-list-versions.md](s3-list-versions.md) — the composable single-level version of this command
- [../scan-design.md](../scan-design.md) — the underlying engine design (applies to both `scan` commands)
