# `ajl s3 scan` — design deep dive

How scan turns "list a bucket" into parallel work, why its defaults hold up
against hand-tuned delimiter schedules, and what its actual limits are.
Decisions are logged in [DESIGN.md](../DESIGN.md); this is the narrative.

## The problem

`aws s3 ls --recursive` and raw `list-objects-v2` walk a bucket one page at a
time: 1,000 keys per call, each call's continuation token depending on the
last. A 270k-object bucket is 271 *serial* round trips — ~60-80 seconds no
matter how fast your machine is. Nothing about the keyspace requires that:
S3 can list disjoint slices of it concurrently. The problem is knowing where
to cut when you can't see the keys before listing them.

## The engine: keyspace slices on a work queue

A **task** is one listing over a slice of a bucket's keyspace:

```
(bucket, prefix, start_after, end_at)     # start exclusive, end inclusive
```

One bounded worker pool (`--workers`) drains a shared queue of tasks. Objects
stream to stdout the moment a page arrives; work discovered along the way
(child prefixes, split ranges) goes back on the queue. There are no stages
and no barriers — a prefix discovered at level 3 starts listing while level 1
is still paginating, and the pool as a whole is busy until the whole keyspace
is accounted for.

The **partition invariant** makes this safe: every mechanism that creates
child tasks (delimiter fan-out, range splitting) must produce slices that
cover the parent's remaining range exactly once, with no gaps and no
overlaps. Exclusive-start/inclusive-end ranges compose cleanly: adjacent
slices `(a, b], (b, c]` share the boundary key unambiguously (it belongs to
the left slice; the right slice starts strictly after it). Correctness never
depends on *where* boundaries sit — only coverage does — which is what lets
scan place boundaries speculatively.

## The per-task decision loop

Each task runs `list-objects-v2` pages and decides, page by page:

1. **Fits in one page** → emit, done. One API call — the floor.
2. **Delimiter fan** (schedule from `--delimiters`) → emit this level's
   objects, enqueue each `CommonPrefix` with the rest of the schedule.
3. **Over-shatter** — the fan exceeds `--max-fan` (think 50k tiny prefixes of
   10 objects each: 50k calls as a fan-out, ~520 as ranges) → stop enqueueing,
   requeue the remainder of the range for the splitter.
4. **Under-shatter** — the fan is *zero* past `--split-after` pages (a `:`
   schedule level applied to a uuid tail) → this is a de-facto leaf
   serially paginating; same abandon, same splitter path.
5. **Hot leaf** — no delimiter left and still truncated after
   `--split-after` pages → ask the splitter to carve the remaining range
   into parallel slices.

Both abandon paths work mid-listing because pagination is lexicographic:
everything ≤ the last item handled is covered (emitted objects, enqueued
prefixes), so the remainder task simply starts after it.

## The radix splitter (default)

Splitting a range means choosing boundaries inside a keyspace you haven't
read. Fixed schemes (`00-ff`) assume an alphabet and distribution; real
buckets have neither reliably. The radix splitter *discovers* both, using
only `list-objects-v2` primitives:

- **Leapfrog probes.** A `MaxKeys=1` listing after `StartAfter=X` returns the
  first live key past X. Probe once to find a branch's first key, then jump
  `StartAfter` to a *sentinel* just past that branch — the branch prefix
  padded with `U+10FFFF` (the highest code point; S3 keys are UTF-8,
  max 1024 bytes, so the padding provably sorts above every key in the
  branch and below the next branch). Each live branch costs exactly one
  probe; branches that don't exist cost nothing.
- **Binary-search descent.** Keys often share a long literal stem
  (`nrn:global:discourse:displayobject:`, date paths, hash prefixes).
  Walking it one character per probe pair costs ~2 probes/char; instead,
  probe the first remaining key, then bisect on "does anything exist past
  `sentinel(first[:depth])`" — the deepest shared stem in ~log2(keylen)
  probes (~10 for a 36-char stem, was ~72).
- **Alphabet extrapolation.** Because boundaries only affect efficiency,
  never correctness, the splitter assumes the alphabet it found at one
  position repeats at the next (uuids and hashes: it does) and synthesizes
  `chars²` boundaries — 16 hex chars become 256 parallel ranges — with zero
  additional probes. A wrong guess costs a few empty ranges at one call
  each; a right one fans the whole keyspace wide in a single split.

Skew is handled by recursion, not prediction: a range that turns out hot
splits again, descending exactly as deep as the data demands. The
9.5M-keys-under-3-hashes pathology costs a few extra split cycles, not a
serial scan.

`hex2`/`hex3`/`alnum2`/`b64-2` provide fixed boundaries when you know the
alphabet, and `--split-class package.module:ClassName` drops in anything
else — the contract is one method, `split(ctx) ->
[{"start_after", "end_at"}] | None`, so boundaries sampled from a database's
primary keys (the classic fix for a monster bucket) is a ten-line class.

## The fast listing path

Once splitting worked, profiling a 230k-object scan showed workers idle at
any concurrency: **botocore's response parser burns ~100ms of GIL-holding
CPU per 1,000-key page** (generic shape walking plus constructing tz-aware
datetimes that ajl immediately serializes back to strings). One Python
process capped at ~10 pages/s regardless of workers.

`FastLister` (default; `--no-fast` reverts) signs `ListObjectsV2` GETs with
botocore's `S3SigV4Auth` (S3 rejects plain SigV4 — it requires
`x-amz-content-sha256`), sends them over a pooled `requests` session with
exponential-backoff retries, and parses with C `ElementTree` in ~3ms/page.
`LastModified` stays the ISO string S3 sent. Responses use
`encoding-type=url` and unquote keys, matching botocore. Tag fetches and
everything non-listing stay on the real boto3 client.

## Reliability at scale

- **A silently missed prefix is an invisible hole in an inventory**, so
  failed tasks stream to `--failed-out` as JSONL seed lines with
  `StartAfter` advanced to the last emitted key — a re-run via
  `--params-json failed.jsonl` scans only what's missing.
- End-of-run stats (tasks/calls/objects/splits/abandons/failures) make
  "did I get everything?" answerable; a live tqdm line on stderr shows
  progress while results stream.
- boto3 clients use adaptive retries; the fast path retries 5xx/429 with
  jittered backoff. Per-request failures never kill the stream — exit code
  reports them at the end.

## Measured results

270k-object CloudTrail-style bucket, run from CloudShell:

| approach | wall time |
|---|---|
| `aws s3 ls --recursive` | 79.0s |
| `aws s3api list-objects-v2` (serial pagination) | 63.6s |
| `ajl s3 scan` defaults, `--workers 32` | **12.5s** (605 calls, 14 splits) |
| `ajl s3 scan` hand-tuned 6-level `/` schedule | 12.0s (526 calls) |

The defaults land within noise of the hand-tuned schedule — the design goal
of adaptive splitting. On a bucket whose keys share a long `:`-separated
stem, an explicit schedule still wins (~35s vs ~52s from a constrained
1-vCPU shell): descending a known stem costs 4 one-call listings, discovery
costs probe cycles. Structure knowledge remains worth expressing when you
have it.

## Known walls, in the order you'll hit them

1. **Serial pagination** — what scan exists to remove.
2. **Response parsing (GIL)** — removed by the fast path.
3. **The wire.** A 1,000-key page is ~300-400KB of XML that S3 neither
   compresses nor lets you trim. 230k objects ≈ 80MB: an ~18-20s floor on a
   ~40Mbit/s connection with 3.8s of CPU, at any worker count. On fat pipes
   the fast path keeps scaling. (CloudShell is a 1-vCPU free-tier container
   with modest network — closer to a laptop than to a real EC2 instance.)
4. **One Python process** tops out around 20-30k objects/s emitted
   (serialization + one stdout). Beyond that, shard.

## Playbook: the billion-object bucket

Math first: 8B objects = ~8M LIST calls (~$40 at $0.005/1k) and ~2.4TB of
response XML. At a single process's ~20-30k obj/s ceiling that's ~4 days;
the bucket's S3 index partitions and your NIC will have opinions too. Shard
it:

```shell
# 1. one cheap call per level: enumerate top-level prefixes
ajl s3 list s3://big-bucket --delimiter / > prefixes.jsonl

# 2. split into N chunks (by count or by known-hot prefixes)
split -n l/8 prefixes.jsonl chunk-

# 3. one scan per chunk — separate processes, or separate machines
ajl s3 scan --params-json chunk-aa --workers 64 \
  --failed-out failed-aa.jsonl | zstd > inv-aa.jsonl.zst
```

Each shard is independently resumable via its failed-out file. For keyspaces
where no delimiter helps, seed ranges directly (each line may carry
`StartAfter`/`EndAt`) — e.g. boundaries pulled from a database's ids — or
encode that logic once as a `--split-class`. Run *in-region* (EC2, not
CloudShell): the wire is wall #3 and intra-region bandwidth is free.
