# Design Decisions

This file tracks the significant design decisions made in `ajl` — what was
decided, why, and what the alternatives were. Add a new entry whenever a
decision changes the shape of the output, the CLI contract, or the model
pipeline. Newest entries go at the bottom of each section; superseded
decisions are struck through with a pointer to their replacement, not
deleted.

## Output format

### JSONL (JSON Lines) as the only output format
Every record is one JSON object per line on stdout. This is the core idea of
the tool: streams compose with `jq`, `grep`, files, and — critically — with
`ajl` itself via `--params-json -`. Diagnostics never go to stdout; they go
to stderr, so stdout is always machine-parseable.

### Consistent leading properties: `Type`, `Id`, `Name`, `Arn`, `Tags`
Every shaped resource starts with the same five properties, in that order,
regardless of service. Rationale: downstream consumers (jq filters, OpenSearch
mappings, DynamoDB items) can rely on one schema for identity and tagging
across all of AWS instead of learning each API's field names.

- `Type` is the ARN-style resource type (`ec2:instance`, `s3:bucket`).
- `Id` falls back to the last segment of the `Arn` when the API has no id field.
- `Name` falls back to `Tags.Name` when the API has no name field.
- `Arn` is either taken from a response field or built from an `arn_format`
  template (`{partition}`, `{region}`, `{account}`, resource fields, and
  root-response scalars as `{root_<Field>}`).
- `Tags` is always a `{Key: Value}` map — AWS's `[{Key, Value}]` tag lists are
  converted so `.Tags.Name` works in jq. Lowercase (`key/value`) and
  `TagKey/TagValue` variants are handled too.

### Collisions with normalized properties keep the original as `Original<Key>`
When a resource has its own field named e.g. `Type` (a VPN gateway's `Type`)
that differs from the normalized value, it is preserved as `OriginalType`
rather than silently dropped or allowed to clobber the contract.

## Output shaping

### Declarative `output.resources` config, with a hand-written jq escape hatch
Shaping lives in per-service model files (`src/ajl/models/<service>.json`),
not in Python code. Each operation can carry:

1. `output.resources` — a declarative config (list `path`, `type`, `id`/
   `name`/`arn` field mapping, `arn_format` template, `tags` field,
   `scalar_as` wrapper for scalar lists) applied by a single generic
   normalizer (`src/ajl/normalize.py`). This covers the vast majority of
   list/describe operations without any code.
2. `output.jq` — a hand-written jq program for APIs whose shape needs more
   (ec2 `DescribeInstances` carrying reservation context onto each instance,
   s3 `ListObjectsV2` emitting `CommonPrefixes` as pipeable `s3:prefix`
   records). **When both are present, the jq program wins.**

Rationale: declarative configs are cheap to add and audit; jq keeps odd APIs
from forcing special cases into the normalizer. jq programs may reference
`$account`, `$region`, `$partition` (bound from the session; the STS account
lookup only happens when the program actually uses `$account`).

### Heuristic fallback for unconfigured operations
An operation with neither `resources` nor `jq`: if the response has exactly
one top-level list, stream its items; otherwise emit the whole page. This
makes every boto3 operation usable on day one, with curation improving the
output later. `--no-parse` bypasses all shaping.

## Model pipeline

### Models generated from botocore's bundled definitions (preferred)
`tools/generate-model.py` reads the `service-2.json` / `paginators-1.json`
data shipped inside the installed boto3. No network, no git clone, and the
input member types always match what the client actually accepts.

~~The original approach extracted models from a clone of aws-sdk-go
(`tools/aws-model-extraction.sh`)~~ — kept for exploration/reference, but
generation from botocore is the source of truth.

### Curated shaping lives in `tools/apply-resource-configs.py`
Regenerating a model file wipes any hand-curated shaping, so the curated
`output.resources` configs and `output.jq` programs live as data in
`tools/apply-resource-configs.py` and are re-applied after every
regeneration. The script — not the JSON files — is the durable home of the
curation. Editing a model file's shaping directly is a mistake that the next
regeneration will silently revert.

### Packaged models, overridable for development
Model files ship inside the wheel (`ajl/models/*.json`, declared as
setuptools package data) and are loaded via `importlib.resources`. The
`AJL_MODELS_DIR` environment variable overrides the lookup for local model
development without reinstalling.

## CLI contract

### aws-cli-style invocation, model-driven coercion
`ajl <service> <operation> --kebab-case-param value` mirrors the aws cli.
Kebab-case is converted to the PascalCase boto3 expects, and values are
coerced using the model's input member types (int/float/bool/list/structure/
map, with JSON parsing where the type calls for it). A flag with no value
becomes `True`; repeated values become a list.

### Pagination on by default
`iter_pages` prefers botocore's generated paginators (they exist for nearly
every list operation and adapt to new APIs automatically). For operations
botocore can't paginate, a marker loop driven by the `input.markers` /
`output.markers` metadata in the model files takes over (matching `X` →
`X` or `NextX` → `X`). `--no-paginate` forces a single call.

### `--params-json` fan-out: records are re-pipeable by construction
JSONL request params stream from a file or stdin onto a thread pool
(`--workers`, default 8; 1 preserves input order). Each line may override
`client`/`operation`/`profile`/`region`. Params the target operation does not
accept are **dropped** (filtered against the model's input members), so
records emitted by one `ajl` stage — carrying `Type`, `Arn`, `Tags`, etc. —
pipe straight back into the next stage with no jq cleanup. A single command
can therefore fan out across prefixes, accounts, and regions.

### Failures are per-request, reported on stderr, exit code 1
In fan-out mode one bad line doesn't kill the stream: the error is printed to
stderr, the other workers keep going, and the process exits 1 at the end if
anything failed.

## Concurrency

### One process, threads, line-atomic writes
boto3 calls are I/O-bound, so threads (not processes/async) are enough.
`Runner` caches boto3 sessions and clients per `(profile, region)` behind a
lock — boto3 `Session` construction is expensive and clients are threadsafe
once built. `Emitter` holds a lock around each write so concurrent workers
never interleave partial lines.

### `--fetch-tags`: batched, overlapped, order-preserving per batch
Resources that already have tags pass through untouched. Tag-less resources
with an `Arn` buffer into batches of 100 (the Resource Groups Tagging API
`GetResources` limit) keyed by session, fetched on background threads so tag
lookup overlaps with fetching the next pages, and emitted in batch submission
order. Tag fetch failures are best-effort: the records still emit, with empty
`Tags`.

## Packaging & tooling

### `src/` layout, setuptools backend, `uv` for everything else
The package lives in `src/ajl` (import isolation from the repo root), built
with setuptools + wheel, dependencies and publishing managed with `uv`.
Python ≥ 3.10. `orjson` is used for serialization speed (with `default=str`
so datetimes from boto3 never crash a stream). Dev tools (`aws`, `uv`, `jq`,
`yq`) are pinned via `mise.toml`.

---

## Decision log template

```markdown
### <Short imperative title>
<What was decided and why — 2-6 sentences. Name the alternative(s) rejected
and the constraint that drove the choice. If this supersedes an earlier
entry, strike that entry through and link here.>
```
