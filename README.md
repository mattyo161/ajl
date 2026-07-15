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
```

Records emitted by `ajl` can be piped straight back into `--params-json -`:
params that the target operation does not accept (like `Type`, `Arn`, `Tags`)
are dropped automatically.

Is a wrapper for the AWS Boto3 API that is intended to be able to replicate as best as possible the `aws` cli tool with ability to stream the output as jsonline (nd-json - each line represents a JSON object). This is a very powerful abstraction that can provide some very fast processing of data from the API. On top of that it allows jsonl data to be passed to `ajl` making it possible to stream 100s/1000s of API calls per second, which is extremely useful when dealing with large data sets like S3 buckets, Tags, etc. It is also a great way to inventory an AWS account and taking the streams of JSONL and saving them into OpenSearch, DynamoDB, DocumentDB, or any other JSON schemaless store or even just saving to S3. 

Objects have a consistent interface with the following properties always set first: `Type`, `Id`, `Name`, `Arn` and `Tags`. `Type` is the ARN-style resource type (e.g. `ec2:instance`, `s3:bucket`). Resource-specific fields like `SecurityGroupId`/`SecurityGroupName` are duplicated into `Id`/`Name`; when there is no `*Name` property `Name` falls back to `Tags.Name`, and `Id` falls back to the last segment of the `Arn`. Ideally the `Arn` should be globally unique value across all of AWS, `Id` and `Name` will depend on the API and how the user configured the AWS account.

The shaping is declarative: each operation in `src/ajl/models/<service>.json` can carry an `output.resources` config (list path, type, id/name/arn field mapping, ARN template, tags field) that a generic normalizer applies, or a hand-written `output.jq` program for APIs whose shape needs the escape hatch (the jq wins when both are present). Operations with neither are unwrapped heuristically (a response with exactly one top-level list streams its items). The configs are applied to the generated model files by `tools/apply-resource-configs.py`.

The `Tags` property will be a map of all the tags for the given resource. Many APIs return the Tags in the response as `{Key:string, Value:string}` pairs which is less useful. So `ajl` will convert them from an array to a map `{Key: Value}` which will allow direct access via `jq` or other json parsers using dot notation like `.Tags.Name` for instance. For APIs that do not return the Tags `ajl` will optionally make calls to the `resourcetaggingapi` to get the tags by `Arn`. These calls will be run in parallel ensuring that responses are fast and efficient.

## Performance

Using `ajl` I have found performance compared to `aws-cli` to be amazingly fast. Performance versus making direct calls to the boto3 is likely comparable, with perhaps some slight overhead due to conversion of the output data to JSON line, however the streaming benefits, the ability to make calls with simple JSON objects, did I mention it will support profiles & regions in your calls so a single command can process many accounts and regions.

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