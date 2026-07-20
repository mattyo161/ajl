# Tools

Some helpful scripts for getting going with `ajl` to extract details for the AWS APIs among other things.

## Get AWS Model Files

Using the AWS Go SDK we can extract the JSON files that describe the output of all the APIs, this is extremely helpful when trying to understand the APIs and create a JSONL wrapper for them.

This command will clone the aws-go-sdk repository into a `.temp` directory and then proceed to extract the desired details from the API files. The `jq`, `yq`, `git` and `python` tools are required to run this process. You can install them with your favorite package manager.

```shell
./aws-model-extraction.sh
```

## Generate Model JSON for ajl

```shell
files=(
../.temp/aws-models/s3/s3-api.json
)
for file in "${files[@]}"; do
cat ../.temp/aws-models/s3/s3-api.json \
| jq -r '
[
    .operations |
    to_entries[] |
    {
        key, 
        value: {
            name: .key,
            input: {
                required: .value.input.required,
                members: (
                    .value.input.members |
                    [   
                        .//{} | # <- Make sure that `null` input is supported as empty map
                        to_entries[] |
                        {
                            key,
                            value: {
                                type: .value.type,
                                name: .key,
                                shape_name: .value.shape_name
                            }
                        }
                    ] | from_entries
                )
            },
            output: {
                members: (
                    .value.output.members |
                    [   
                        .//{} | # <- Make sure that `null` input is supported as empty map
                        to_entries[] |
                        {
                            key,
                            value: {
                                type: .value.type,
                                name: .key,
                                shape_name: .value.shape_name
                            }
                        }
                    ] | from_entries
                )
            }
        }
    }
] | from_entries 
' \
> "${file%.*}-model.json"
done
```

## Generate model files from botocore (preferred)

Generates `src/ajl/models/<service>.json` straight from the service
definitions bundled with the installed boto3 — no git clone or network
needed, and the input member types always match what the client accepts:

```shell
uv run python tools/generate-model.py lambda iam sns ...
uv run python tools/generate-model.py --all-existing   # regenerate everything
```

Always run `tools/apply-resource-configs.py` afterwards to re-apply the
curated output shaping.

## Re-apply ajl resource configs

Regenerating the model files wipes the curated output shaping. Re-apply the
declarative `output.resources` configs and hand-written `output.jq` programs
with:

```shell
python3 tools/apply-resource-configs.py
```

## Analyze an inventory run with DuckDB

`tools/build-duckdb.py` loads a `tools/inventory.sh` run's output (and, if
present, `--api-log`'s telemetry) into a DuckDB file for ad-hoc SQL —
security review, performance review, or just exploring what an account
looks like. Standalone script, not a dependency of `ajl` itself; runs via
its own PEP 723 header (`uv run` installs `duckdb` into an ephemeral env,
nothing touches this project's `pyproject.toml`):

```shell
uv run bash tools/inventory.sh          # produces .temp/data/*.jsonl
uv run tools/build-duckdb.py            # -> .temp/inventory.duckdb
```

`tools/security-checks.sql` is a set of generic, reusable monitoring
queries against the tables this produces (open security groups, public EKS
endpoints, expired certs, stale IAM keys, missing Multi-AZ, ...) — no
account IDs or customer names hardcoded, safe to keep in a public repo and
re-run after remediation work to check progress:

```shell
duckdb .temp/inventory.duckdb < tools/security-checks.sql
```

See [docs/duckdb-analysis.md](../docs/duckdb-analysis.md) for query
patterns and gotchas (struct field casing, CloudTrail's JSON-as-string
columns, isolating one run's slice of the cumulative apilog).
