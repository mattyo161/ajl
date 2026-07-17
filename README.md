# ajl - AWS JSON Line

## Documentation

- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, model curation workflow, tests, releasing
- [STYLE_GUIDE.md](STYLE_GUIDE.md) — code and output conventions
- [DESIGN.md](DESIGN.md) — design decisions and the reasoning behind them
- [AGENTS.md](AGENTS.md) / [CLAUDE.md](CLAUDE.md) — instructions for AI coding agents
- [tools/README.md](tools/README.md) — model generation tooling

## Installation

Requires Python ≥ 3.10 and configured AWS credentials (a profile,
environment variables, or an instance role — anything boto3 understands).

Install as a standalone tool straight from GitHub with
[uv](https://github.com/astral-sh/uv) (or swap `uv tool install` for
`pipx install`):

```shell
uv tool install git+https://github.com/mattyo161/ajl
ajl s3 list-buckets
```

Or from a local clone, which is also the development setup:

```shell
git clone https://github.com/mattyo161/ajl
cd ajl
uv tool install .     # install the ajl command globally
# — or —
uv sync               # just a .venv for development
uv run ajl s3 list-buckets
```

A preview build is also on TestPyPI (may lag behind the repo). Dependencies
live on regular PyPI, so include it as an extra index:

```shell
pip install --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ ajl
```

## Usage

```shell
# any boto3 service/operation, aws-cli style kebab-case params
ajl ec2 describe-instances
ajl ec2 describe-security-groups --filters '[{"Name":"vpc-id","Values":["vpc-123"]}]'

# pagination is on by default and results stream as JSONL
ajl s3 list-objects-v2 --bucket my-bucket --max-items 100
ajl rds describe-db-instances --no-paginate

# fetch missing tags in batches of 100 via the Resource Groups Tagging API
ajl s3 list-buckets --fetch-tags

# stream request params as JSONL for 100s of parallel calls (--workers)
# each line may set client/operation/profile/region or rely on the defaults
ajl s3 list-objects-v2 --bucket my-bucket --delimiter / \
  | jq -c 'select(.Type == "s3:prefix")' \
  | ajl s3 list-objects-v2 --params-json - --workers 16

# post-shaping jq on any command (empty output drops, strings print raw)
ajl ec2 describe-instances --jq 'select(.State.Name == "running") | .Uri // .Arn'

# stamp records with Profile/Region/Account so later stages reuse the session
ajl s3 list-buckets --profile prod --stamp-session

# single-level listing with pipeable prefixes: the classic fan-out dance
ajl s3 list s3://my-bucket --delimiter / \
  | ajl s3 list --params-json - \
  | ajl s3 list --params-json - --jq 'del(.Delimiter)' \
  | ajl s3 list --params-json - --workers 32

# inventory a whole bucket: delimiter fan-out + adaptive range splitting
ajl s3 scan s3://my-bucket --delimiters '/ / /' --workers 64 \
  --failed-out failed.jsonl
ajl s3 scan --params-json failed.jsonl --workers 64   # re-run only the misses

# batch the stream into delete-objects calls, 1000 keys per request
ajl s3 scan s3://my-bucket/tmp/ \
  | parallel --pipe -N1000 "jq -sc '{Delete: {Objects: map({Key: .Id}), Quiet: true}}'" \
  | ajl s3 delete-objects --bucket my-bucket --params-json -
```

### `ajl ssm get`

```shell
ajl ssm get --name /app/db/host                 # one param (plaintext)
ajl ssm get --names /a /b /c ...                 # many (chunked 10/call, parallel)
cat names.txt | ajl ssm get --names -            # names from stdin, streamed
ajl ssm get --path /app --recursive              # a whole hierarchy
ajl ssm params --cache 15m                       # describe-parameters, throttle-proof + cached
```

The API is chosen by the argument (`get-parameter` / `get-parameters` /
`get-parameters-by-path`); decryption is on by default (`--no-decryption` to
disable — the flag never reaches the API). SecureString values are **sealed
inline** so a bulk extract piped to a file never carries plaintext secrets:
single `--name` prints plaintext (you asked for one; `--encrypt` seals it),
`--names`/`--path` seal by default (`--decrypt` forces plaintext). Sealing
needs an age key (`AJL_AGE_*`); use x25519 identity mode, not passphrase, for
bulk (passphrase scrypt is deliberately slow).

```shell
# sealed on the wire — safe to save; reveal only when you mean to
ajl ssm get --path /prod --recursive > secrets.jsonl   # SecureStrings sealed
ajl decrypt < secrets.jsonl                             # unseal AJLSEC values inline
```

Two orthogonal protections: the **cache file** is whole-file age-encrypted
(ssm/secretsmanager + `--cache` *requires* a key, else it refuses), and
**output values** are field-sealed independently so the stdout stream itself
is safe to pipe or store.

## Caching, encryption, and the learn log

```shell
# serve results younger than 15m from ~/.cache/ajl (gzipped); else run + store
ajl ssm describe-parameters --cache 15m
ajl ssm describe-parameters --cache 15m          # instant: zero API calls
export AJL_CACHE=15m                             # make caching the default

# age-encrypt the cache with a per-session key (lose the key? it just re-runs)
export AJL_AGE_IDENTITY="$(ajl cache keygen 2>/dev/null | grep AGE-SECRET-KEY)"

ajl cache ls                # entries as JSONL: command, age, lines, encrypted
ajl cache clear [--all]     # expired entries (default) or everything
ajl x y --refresh           # skip reading the cache, still store fresh
ajl x y --rm-after 1h       # entry lifetime before auto-cleanup (default 7d)

# the learn log: see the aws-cli equivalent, keep an audit/perf record
ajl ec2 describe-instances --learn
# stderr: ajl: [learn] aws ec2 describe-instances
# and one JSONL record (argv, aws equivalent, duration, exit code, cache
# status, scan slice samples) appended to ~/.local/state/ajl/learn.jsonl
export AJL_LEARN=1                               # always on
```

Cache hits always print one stderr notice (age, line count, seconds saved) so
you can never unknowingly analyze stale data — and stdout stays byte-identical
to the original run. Every internal cache (boto3 sessions/clients, account
ids, jq programs, models) reports hits to stderr under `AJL_DEBUG_CACHE=1`.

### `ajl s3 list` and `ajl s3 scan`

Both emit **lean records** built for volume: `Uri` (`s3://bucket/key`)
replaces `Id`/`Name`/`Arn`, and `Tags` appears only under `--include-tags`
(one `get-object-tagging` call per object). A live progress line shows on
stderr while results stream (auto-off when stderr isn't a terminal, or with
`--no-progress`). The generic `ajl s3 list-objects-v2` keeps the full
five-property contract.

`list` is the composable single-level primitive: one `list-objects-v2` per
seed, `CommonPrefixes` emitted as `s3:prefix` records that pipe straight back
into the next `ajl s3 list --params-json -`. Each prefix record carries its
`Delimiter`, so every pipe stage repeats the grouping one level deeper until a
`--jq 'del(.Delimiter)'` makes the final stage list its prefixes recursively —
with `--workers` fanning the requests out in parallel.

`scan` inventories buckets orders of magnitude faster than `list-objects-v2
--recursive`-style listings by turning the keyspace into parallel work: one
bounded worker pool (`--workers`) drains a queue of listings, `CommonPrefixes`
found via the `--delimiters` schedule go back into the queue, and "hot"
prefixes that keep paginating past `--split-after` pages are carved into
parallel key ranges by a splitter. The default `radix` splitter discovers the
key alphabet actually in use with StartAfter leapfrog probes (it descends long
shared hash prefixes instead of scanning them serially); fixed splitters
(`--split hex2|hex3|alnum2|b64-2`) cut at precomputed boundaries; and
`--split-class package.module:ClassName` drops in your own strategy for
tomorrow's pathological bucket. A delimiter level that over-shatters (more
than `--max-fan` child prefixes) automatically falls back to range splitting.
Failed listings stream to `--failed-out` as re-runnable `--params-json` seeds.

Records emitted by `ajl` can be piped straight back into `--params-json -`:
params that the target operation does not accept (like `Type`, `Arn`, `Tags`)
are dropped automatically.

Is a wrapper for the AWS Boto3 API that is intended to be able to replicate as best as possible the `aws` cli tool with ability to stream the output as jsonline (nd-json - each line represents a JSON object). This is a very powerful abstraction that can provide some very fast processing of data from the API. On top of that it allows jsonl data to be passed to `ajl` making it possible to stream 100s/1000s of API calls per second, which is extremely useful when dealing with large data sets like S3 buckets, Tags, etc. It is also a great way to inventory an AWS account and taking the streams of JSONL and saving them into OpenSearch, DynamoDB, DocumentDB, or any other JSON schemaless store or even just saving to S3. 

Objects have a consistent interface with the following properties always set first: `Type`, `Id`, `Name`, `Arn` and `Tags`. `Type` is the ARN-style resource type (e.g. `ec2:instance`, `s3:bucket`). Resource-specific fields like `SecurityGroupId`/`SecurityGroupName` are duplicated into `Id`/`Name`; when there is no `*Name` property `Name` falls back to `Tags.Name`, and `Id` falls back to the last segment of the `Arn`. Ideally the `Arn` should be globally unique value across all of AWS, `Id` and `Name` will depend on the API and how the user configured the AWS account.

The shaping is declarative: each operation in `src/ajl/models/<service>.json` can carry an `output.resources` config (list path, type, id/name/arn field mapping, ARN template, tags field) that a generic normalizer applies, or a hand-written `output.jq` program for APIs whose shape needs the escape hatch (the jq wins when both are present). Operations with neither are unwrapped heuristically (a response with exactly one top-level list streams its items). The configs are applied to the generated model files by `tools/apply-resource-configs.py`.

The `Tags` property will be a map of all the tags for the given resource. Many APIs return the Tags in the response as `{Key:string, Value:string}` pairs which is less useful. So `ajl` will convert them from an array to a map `{Key: Value}` which will allow direct access via `jq` or other json parsers using dot notation like `.Tags.Name` for instance. For APIs that do not return the Tags `ajl` will optionally make calls to the `resourcetaggingapi` to get the tags by `Arn`. These calls will be run in parallel ensuring that responses are fast and efficient.

## Performance

Real numbers from a 270k-object CloudTrail log bucket (run from AWS
CloudShell — a constrained 1-vCPU environment, not a tuned instance):

| approach | wall time |
|---|---|
| `aws s3 ls --recursive` | 79.0s |
| `aws s3api list-objects-v2` (serial pagination) | 63.6s |
| `ajl s3 scan` **defaults**, `--workers 32` | **12.5s** — 21,700 obj/s |
| `ajl s3 scan` hand-tuned 6-level `/` schedule | 12.0s |

Two things worth noticing: the zero-config default (adaptive radix
splitting) lands within noise of a hand-tuned delimiter schedule, and the
whole scan cost 605 API calls for 270,108 objects — barely above the
271-page theoretical floor, just made in parallel instead of in series.
And the cold-start story is short: on a fresh CloudShell,
`uv tool install git+https://github.com/mattyo161/ajl` to first inventory
line is under a minute.

The full design — how splitting works without seeing the keys first, why
there's a raw-HTTP fast path, where the real walls are (spoiler: eventually
your NIC), and the playbook for billion-object buckets — is in
[docs/scan-design.md](docs/scan-design.md).

My hope is that putting this out into the community there will be opportunities to take this concept much further then the general PoCs that I have been messing with over the years working with AWS.

## Publishing with `uv`

```shell
uv init 
uv add requests
uv add --dev pytest
uv build
uv publish --index testpypy --token ****
```

```shell
cd /temp
mkdir test
cd test

uv venv
uv pip install -i https://test.pypi.org/simple/ ajl

```