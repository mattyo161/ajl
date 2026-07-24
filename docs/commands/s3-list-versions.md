# `ajl s3 list-versions`

Same engine, flags, and output shape as [`ajl s3 list`](s3-list.md), but
speaks `list-object-versions` instead of `list-objects-v2` — object
versions and delete markers instead of current objects only, at the same
raw-signed-HTTP speed. A separate command rather than a `--versions` flag
on `list`, so `list`'s existing (and heavily-optimized) code path is
provably untouched. Source: `src/ajl/scan.py`
(`build_list_versions_parser`, `run_list_versions`).

## Auto-detection

Before listing starts, one `get-bucket-versioning` call per distinct
bucket among the seeds decides whether it's actually worth paying for
`ListObjectVersions`. A bucket that's never had versioning enabled falls
back to the exact same fast path `ajl s3 list` uses — no wasted calls, and
no different result either way (an unversioned bucket only ever has one
"version" per key).

| Flag | Default | Notes |
|---|---|---|
| everything from [`s3 list`](s3-list.md) | | unchanged |
| `--force-versions` | off | skip the `get-bucket-versioning` check, always list as versions — use this if the check itself is denied (a failed check otherwise defaults to *not* versioned, with a stderr warning, so a permissions gap fails loud rather than silently missing history) |

## Output shape

Two record types instead of `s3:object`, otherwise the same lean
`ajl.uri`-carries-identity contract as `list`:

```json
{"Bucket": "my-bucket", "Key": "logs/x.log", "VersionId": "3sL4kqt...",
 "IsLatest": true, "LastModified": "...", "ETag": "\"...\"", "Size": 8421,
 "StorageClass": "STANDARD",
 "ajl": {"type": "s3:object-version", "uri": "s3://my-bucket/logs/x.log"}}
{"Bucket": "my-bucket", "Key": "logs/old.log", "VersionId": "A1B2C3...",
 "IsLatest": true, "LastModified": "...",
 "ajl": {"type": "s3:delete-marker", "uri": "s3://my-bucket/logs/old.log"}}
```

A delete marker has no `ETag`/`Size`/`StorageClass` (there's no object
behind it) and `--include-tags` skips it entirely — nothing to tag.
`--include-tags` on an `s3:object-version` record fetches that *specific*
version's tags (`get-object-tagging` with `VersionId` set), not just the
current one.

## Examples

```shell
ajl s3 list-versions s3://my-bucket
ajl s3 list-versions s3://my-bucket --jq 'select(.ajl.type=="s3:object-version")'  # skip delete markers
ajl s3 list-versions s3://my-bucket --force-versions  # get-bucket-versioning access denied
```

## See also

- [s3-list.md](s3-list.md) — the plain (current-objects-only) version of this command
- [s3-scan-versions.md](s3-scan-versions.md) — the recursive version of this command
