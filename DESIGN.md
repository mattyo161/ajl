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

## CLI contract (continued)

### Global `--jq` post-filter, applied as an emitter wrapper
`--jq PROGRAM` runs on every record after shaping (and after `--stamp-session`
stamping, before `--fetch-tags` buffering, so filtered-out records never cost
a tagging call). Empty output drops the record, multiple outputs emit multiple
lines, string outputs print raw (jq `-r` style). The program gets no `$vars` —
records that need account/region context should be stamped first, which puts
those values in the record itself.

### `--stamp-session`: records carry their credentials
Opt-in stamping adds `Profile`/`Region`/`Account` to every record, and
`--params-json` accepts the PascalCase forms as session routing fields
(popped, never sent to the API). This closes the multi-account loop: a stream
mixing sessions can be piped back in and each line routes to the session that
produced it.

### `Uri` on s3 records
All curated s3 shapes emit `Uri: s3://bucket/key` via a declarative
`uri_format` template (same variables as `arn_format`). URIs are the
composable s3 addressing scheme: seeds for `scan`/`list`, input to other s3
tooling, and unambiguous across buckets.

## s3 scan / s3 list

Narrative deep dive (architecture, splitter math, measured results, the
billion-object playbook): [docs/scan-design.md](docs/scan-design.md).

### Lean records on the high-volume path
`scan` and `list` records are `{Type, Uri, Bucket, ...api fields}` — a
deliberate exception to the five-property contract. At inventory volumes
`Id`/`Name`/`Arn` are pure repetition of `Uri` (billions of records × ~200
wasted bytes), and an always-empty `Tags: {}` is noise: `--include-tags`
opts in, populated for real via one `get-object-tagging` call per object
(expensive, so never the default). The generic `ajl s3 list-objects-v2`
shapes keep the full contract for consumers that want schema uniformity.

### `ajl s3 list`: the composable single-level primitive
The original supplemental-wrapper pattern, kept because pipes are sometimes
the right tool: one `list-objects-v2` per seed, prefixes *emitted* (not
recursed) as `s3:prefix` records carrying their `Delimiter`, so each
`--params-json` stage repeats the grouping one level deeper and a
`--jq 'del(.Delimiter)'` turns the last stage recursive. Implemented as the
same `Scanner` engine with `recurse=False` — workers, session routing,
failed-out and progress come for free.

### Progress on by default when stderr is a terminal
Long scans stream results but the interesting signal (tasks, queue depth,
splits, failures) is invisible until done — so a tqdm counter renders on
stderr (0.5s refresh) whenever stderr is a TTY, `--no-progress` opts out, and
non-TTY runs stay clean for logs/cron. `--verbose` additionally prints plain
progress lines every 5s (grep-able in captured logs).

### Fan-out is a scheduler, not a pipe depth
`ajl s3 scan` replaces N-deep `--params-json` pipelines for bucket inventory:
one bounded worker pool drains a queue of listing tasks, discovered prefixes
re-enter the queue with the remaining `--delimiters` schedule, and results
stream as they arrive. One pool means no per-stage idle workers, bounded
memory, and end-of-run completeness accounting (stats on stderr; failed tasks
stream to `--failed-out` as re-runnable seeds with `StartAfter` advanced to
the last emitted key). Tasks are `(bucket, prefix, start_after, end_at)`
slices with exclusive start / inclusive end, so adjacent slices partition the
keyspace with no gaps and no dups — the correctness invariant every splitter
must preserve.

### Adaptive splitting: radix leapfrog default, fixed ranges opt-in, classes drop-in
A task that keeps paginating past `--split-after` pages asks its splitter to
carve the remaining range into parallel slices; a delimiter fan exceeding
`--max-fan` abandons the delimiter and requeues the remainder for the same
splitter path (50k prefixes × 10 objects costs 50k calls as a fan-out but
~520 as ranges). The default `radix` splitter discovers the live key alphabet
with `StartAfter` leapfrog probes (`MaxKeys=1`, then jump past the branch via
a max-code-point sentinel; S3 keys cap at 1024 bytes) and descends
single-branch levels, so a 15-character shared hash prefix costs ~30 probes
instead of hours of serial pages — no assumption that keys are hex or base64.
Fixed splitters (`hex2`, `hex3`, `alnum2`, `b64-2`) exist for keyspaces where
the alphabet is known; `--split-class package.module:ClassName` loads any
object with `split(ctx) -> [{"start_after", "end_at"}] | None` for shapes not
yet imagined (e.g. boundaries sampled from a database's primary keys).

### s3 clients use adaptive retries
`scan` builds its s3 clients with botocore's adaptive retry mode
(`max_attempts=10`) so throughput self-tunes to the bucket index's SlowDown
responses instead of failing or hammering.

### Fast listing path: raw SigV4 HTTP + C ElementTree (default; `--no-fast`)
botocore's generic response parser costs ~100ms of GIL-holding CPU per
1000-key page (shape walking + tz-aware datetime objects that ajl immediately
stringifies again), capping any number of workers at ~10 pages/s in one
process. `FastLister` signs ListObjectsV2 GETs with botocore's `S3SigV4Auth`
(S3 requires the `x-amz-content-sha256` header — plain SigV4Auth 400s), sends
them over a pooled `requests` session with its own backoff retry, and parses
with C ElementTree in ~3ms — `LastModified` stays the ISO string S3 sent
(cosmetic diff: `.000Z` vs `+00:00`). Responses use `encoding-type=url`, keys
are unquoted on parse (botocore parity). `get_object_tagging` and `meta` still
delegate to the boto3 client. `--no-fast` is the escape hatch for exotic
setups; custom `endpoint_url`s get path-style URLs.

### Delimiter under-shatter: a fan of zero is a leaf
A delimiter task still paginating past `--split-after` pages with **zero**
prefixes found (e.g. a `:` level applied to a plain-uuid tail) is a de-facto
leaf serially scanning the keyspace — the same abandon mechanism as over-
shatter requeues the remainder for the splitter. Before this, a schedule like
`/ : :` on a colon-less tail listed 231 pages serially with splits never
engaging.

### Radix extrapolates its alphabet one level deeper
Range boundaries partition the keyspace wherever they sit, so after
discovering the branch alphabet (e.g. 16 hex chars) the splitter synthesizes
`chars²` boundaries (capped by `fan_target=256`) with zero extra probes — a
right guess (uuids, hashes) fans 256-wide in one split; a wrong one just
yields some empty ranges at one call each. Probe-per-split cost stays
~log2(keylen) via binary-search descent of shared stems (a 36-char literal
prefix costs ~10 probes, not 72).

### Emitter flushes on a 100ms interval, not per line
A flush syscall per record dominated CPU at 200k+ records (the stream is
already the kernel pipe buffer's problem); 100ms max staleness is
imperceptible in a pipe and `flush()` still forces the tail at exit.

### Known floor: the network pipe, not the code
A 1000-key page is ~300-400KB of XML (S3 sends no compression and offers no
field selection), so 230k objects ≈ 80MB on the wire — on a ~40Mbit/s
connection that is an ~18-20s physical floor regardless of workers. Measured:
the same scan that took 40.5s pre-optimization runs at that floor (~21s) with
8 workers and ~3.8s of CPU. On fat pipes (EC2/CloudShell) the fast path keeps
scaling where botocore parsing used to be the ceiling.

### Future: generalize the queue to parent→child API walks
The scheduler pattern (queue + bounded pool + child tasks) applies beyond s3:
ecs cluster → services → tasks, route53 zones → record sets, org accounts →
anything. When a second walker shows up, extract the Scanner core rather than
cloning it.

## Result cache & learn log

### Whole-invocation result cache, opt-in, always announced
`--cache TTL` / `AJL_CACHE` cache the invocation's complete JSONL output,
gzipped under `~/.cache/ajl`, keyed by a hash of everything that shapes the
output (ajl version, service/operation, tokens, output-affecting flags,
resolved profile/region, full `--params-json` content — stdin is spooled and
hashed). Point-in-time API data rarely changes inside 15 minutes, so a hit
replays instantly with zero API calls *and zero credentials*. Hits always
print one stderr notice (age, lines, seconds saved) — silently serving cached
data would invite staleness confusion. Only exit-0 runs are stored; entries
carry an expiry (`--rm-after`, default 7d) and any cache-enabled run sweeps
expired entries. `ajl cache ls|clear|keygen` manage it.

### age encryption via pyrage, keyed from the environment
With `AJL_AGE_IDENTITY` set (an `AGE-SECRET-KEY-...` or a path), cache files
are age-encrypted after gzip — one env var covers both directions since the
recipient derives from the identity (`AJL_AGE_RECIPIENTS` and
`AJL_AGE_PASSPHRASE` cover asymmetric-write-only and symmetric modes). The
session-key story is the point: generate a key per session, and secrets like
org-wide ssm parameter dumps never touch disk in plaintext; an entry the
current key can't open is just a cache miss, and losing the key merely means
re-running the command. pyrage (rust wheels) avoids needing the age binary;
encrypt/decrypt buffers the gzipped payload in memory, so skip `--cache` for
truly giant scans until a streaming path exists.

### The learn log: one record per invocation, teaching plus audit
`--learn` / `AJL_LEARN=1` prints the aws-cli equivalent to stderr up front
(`ajl: [learn] aws ec2 describe-instances --max-results 5`) and appends one
JSONL record to `~/.local/state/ajl/learn.jsonl`: argv, aws equivalent,
profile/region, duration, exit code, cache status, and for scans the run
stats plus a ~12-slice sample of the prefixes/ranges actually listed — the
interesting jumps, never the per-page marker churn. One line per invocation
keeps it a usable audit and performance log.

### Internal cache hits are observable (`AJL_DEBUG_CACHE=1`)
boto3 sessions/clients, STS account ids, compiled jq programs, service
models and scan's s3 clients report cache hits to stderr when the env var is
set — for debugging credential reuse surprises and confirming fan-out runs
reuse clients rather than rebuilding per line.

## ssm wrappers

### `ssm get` picks the API from the argument
`--name` → get-parameter, `--names` → get-parameters (chunked at 10, the API
max, fanned across workers; `-` reads names from stdin), `--path` →
get-parameters-by-path (paginated, `--recursive`). Decryption defaults on;
`--no-decryption` sets `WithDecryption=false` and the flag itself never
reaches the API (which has no such parameter). `ssm params` is a thin alias
for describe-parameters, relying on the now-adaptive Runner clients to ride
its notoriously aggressive throttle, and on `--cache` to make the slow scan a
one-time cost. `ssm get` is a custom command (like s3 scan) so it owns its
record shape and avoids the normalizer's Type-collision on the API's Type.

### Two orthogonal secret protections: cache-at-rest and output-sealing
Secrets are guarded on two independent axes, each simple on its own:
- **Cache at rest** — the whole gzipped cache file is age-encrypted whenever
  a key is configured (existing mechanism). ssm/secretsmanager + `--cache`
  *requires* a key or the run refuses — no accidental plaintext-secret cache.
- **Output/pipe sealing** — SecureString *values* are age-sealed inline as
  `AJLSEC:1:<b64>` envelopes (seal.py, reusing the cache's age config), so the
  stdout stream is safe to pipe to a file / `tiss sd` / a screen regardless of
  caching. Independent of the cache; protects the stream itself.

Because they're orthogonal, no split-tee or per-path transform is needed: the
command seals values before emitting, the cache tee stores whatever is
emitted, and a cache hit replays identical bytes. Default output modes:
single `--name` plaintext (deliberate lookup; `--encrypt` to seal),
`--names`/`--path` sealed (bulk; `--decrypt` to force plaintext). Bulk sealing
requires a recipient or it errors toward `--decrypt`.

### `ajl decrypt`: standalone unseal filter
A pseudo-command (like `ajl cache`) that reads JSONL from stdin and unseals
every `AJLSEC:` envelope in place — for `restored-secrets | ajl decrypt`. It's
a command, not a `--decrypt` global flag, to avoid clashing with `ssm get`'s
own `--decrypt`/`--encrypt` subparser flags.

### Rejected: value digest/hash tags on parameters
A `--digest` writing `sha256(value)` to a readable Tag was considered and
dropped: tags are readable via ListTagsForResource *without* decrypt
permission, so a hash of a low-entropy secret is an offline-guessing oracle
that weakens SecureString. Change-detection for cache refresh needs no tag —
SSM's native `Version`/`LastModifiedDate` cover it. Tamper-evidence, if ever
needed, is a keyed MAC (HMAC or KMS GenerateMac/VerifyMac), not a bare hash —
tabled for now.

### Clients use adaptive retries by default
`Runner.client` now builds every client with botocore adaptive retry mode
(max 10 attempts), not just scan's s3 clients — throttle-heavy APIs
(describe-parameters above all) self-tune instead of failing.

### Version derived from git tags (setuptools-scm)
The package version comes from git tags (`v0.2.0` → `0.2.0`) via
setuptools-scm instead of a hand-bumped field — the version previously lived
in both `pyproject.toml` and `__init__.py` and the two could drift. Releasing
is now `git tag vX.Y.Z`; untagged builds get a `.devN+g<sha>` suffix so they
can never be mistaken for releases. `ajl.__version__` reads the generated
`src/ajl/_version.py` (gitignored), with a dev fallback for uninstalled
source trees.

### Per-API-call telemetry via botocore event hooks, not a wrapper layer
`--api-log`/`AJL_APILOG=1` (`apilog.py`) logs one JSONL record per underlying
botocore call by registering `before-call`/`after-call`/`after-call-error`
handlers on each client in `Runner.client()`, instead of wrapping/timing
`run_operation`'s call sites. botocore's event emitter is hierarchical, so
one registration on the bare event name catches every operation a client
makes, at the layer closest to the wire (HTTP status, retry attempt count
from the same `context` dict botocore's own retry logic mutates). Measured
limitation: each boto3 client gets its own independent copy of the event
emitter (confirmed against botocore 1.40), so this does not see
credential-resolution sub-clients boto3 builds internally (SSO
`GetRoleCredentials`, an `AssumeRole` fetcher) — only calls made through
clients `Runner.client()` itself builds. A gap in the log on an otherwise
slow request is itself a useful signal: it points at credential resolution
rather than the call ajl made.

### `--version` asks git directly instead of trusting the baked-in version
Discovered while building this: the setuptools-scm `_version.py` (see above)
only regenerates on build/install, so in this repo's edit-then-`uv run`
dev loop it silently goes stale — observed reporting a commit 10 commits
behind actual `HEAD` with no reinstall in between. `--version`
(`runtime_version()` in `main.py`) instead walks up from `main.py`'s own
file location looking for a `.git` directory; when found (an editable
install pointed at this checkout) it shells out to `git describe --tags
--always --dirty` for a live, always-accurate answer (tag, commits-since,
short sha, `-dirty` if uncommitted). Falls back to the packaged
`__version__` when there's no adjacent `.git` — a real installed
distribution, where the baked-in version isn't stale because there's no
"ahead of the last build" state to drift from.

### `--params-json` always stamps the session; input keys remap to the model's real casing
Found chaining a 3-stage `ecs` pipeline (`list-clusters --all-regions | ... |
list-tasks --params-json - | ... | describe-tasks --params-json -`): the
Profile/Region/Account stamp a fan-out stage attaches only survived one hop.
`--stamp-session` was opt-in per invocation, so every *middle* stage of a
piped chain silently dropped the session info its own input carried unless
the user remembered `--stamp-session` at every single stage. Fixed by
`should_stamp_session()`: a `--params-json` stage is by definition
mid-pipeline, so it now stamps by default too (fan-out still originates it;
`--no-stamp-session` opts back out).

Same investigation surfaced a second, independent bug: `coerce_params`
always guessed a request param's real name by PascalCasing the CLI flag
(`--cluster` -> `Cluster`), which boto3 rejects outright for the handful of
APIs — ecs among them — whose input members are lowerCamelCase (`cluster`,
`tasks`). Fixed by remapping every incoming key to the operation's actual
member casing (case-insensitive lookup) before coercion/filtering, so both a
direct `--cluster` flag and a piped record's field resolve regardless of
which casing convention the target API uses.

### `--stamp-session` also carries the request params a response doesn't echo back
Continuing the ecs pipeline work: `list-tasks --cluster my-cluster` streams
task ARNs, and the response never repeats which cluster they came from — a
downstream `--params-json` stage (or storage) had no way to know without
re-parsing it out of the task ARN. `run_operation` now merges the resolved
request params onto every emitted record when `--stamp-session` is active
(`_stamp_params()`, `main.py`), using `setdefault` so a genuine response
field is never clobbered by a same-named request param. Kept as one flag
rather than a second one, since both are the same idea — attach whatever
context a bare response doesn't carry — and the request already asked for
`--stamp-session` to fix exactly this class of problem for Profile/Region/
Account.

### `--describe`: a declarative List→Describe pairing, not per-service magic
Surveyed every onboarded service's real botocore models for List operations
that only return bare ids/arns paired with a Describe/Get operation that
takes them back. Two findings shaped this: (1) it's ~65 pairs across 20 of
36 services — common enough to be worth a generic mechanism, not a one-off
for ecs; (2) the *dominant* shape (~85%) is a **singular** describe — one
call per id, no batch form at all (`eks`, `iam`, `sagemaker`, `transfer` are
entirely this shape) — batch/array describes (`ecs`, `ecr`, `athena`,
`cloudtrail`, `cloud9`, `opensearch`) are the minority. Neither which List
pairs with which Describe nor a batch operation's real max-items-per-call
is discoverable from the botocore model (checked: no `min`/`max` metadata on
`ecs.DescribeTasks`'s `tasks` member) — both have to be curated, the same as
`output.resources` already is.

So `--describe` is driven by a new declarative `output.describe` config on
the List operation (`{operation, id_field, param, kind: scalar|array,
batch_size?, scope?}`), written by a new `d()` helper in
`apply-resource-configs.py` next to `r()`. `run_operation` checks for it
first; if present and `--describe` is passed, `run_describe_chain()` lists,
collects `id_field` off the shaped records, and either calls the describe
op once per id (`kind: scalar`) or chunks ids into `batch_size` groups
(`kind: array`, same chunking `ssm.py` already does for `get --names`) —
either way fanning across the same worker pool `--params-json` uses.
`scope` names List-call input params (e.g. `RoleName`, `cluster`) that the
Describe call also needs but that never appear in the list response, and
are forwarded from the original call's own params — not stamped from a
record, since the record IS the describe result at that point.

Piloted on the two shapes surveyed: `iam.ListRolePolicies` →
`GetRolePolicy` (scalar, needs the `RoleName` scope) and
`ecs.ListClusters`/`ListTasks` → `DescribeClusters`/`DescribeTasks` (array,
`ListTasks` also needs the `cluster` scope). One easy way to get this wrong
found and fixed while piloting: naively reusing `--stamp-session`'s
`_stamp_params()` on the describe result attaches the *entire batch's*
identifier list to every record in it for `kind: array` — redundant and
bloats output at scale. `run_describe_chain` stamps only `scope_params`
(the useful, small, per-batch-constant context), never the identifier
itself.

### Fix: `--describe` wasn't part of the cache key material
Found while documenting the result cache's key material (`ResultCache.key`,
`cache.py`): `--describe` entirely replaces what a List operation emits
(list records -> paired Describe/Get records) but was never added to the
hashed material when it shipped. `ajl iam list-role-policies --role-name x
--cache 1h` and the same command with `--describe` added computed the
*identical* cache key — whichever ran first would get replayed verbatim for
the other, silently serving the wrong shape of data. Added `"describe":
options.describe` to the hashed material, matching how every other
output-affecting flag (`jq`, `stamp_session`, `fetch_tags`, ...) is already
there. A reminder that this list needs a deliberate update whenever a new
flag changes what gets emitted — it is not derived automatically from the
options object.

---

## Decision log template

```markdown
### <Short imperative title>
<What was decided and why — 2-6 sentences. Name the alternative(s) rejected
and the constraint that drove the choice. If this supersedes an earlier
entry, strike that entry through and link here.>
```
