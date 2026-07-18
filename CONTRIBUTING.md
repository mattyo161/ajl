# Contributing to ajl

Thanks for your interest in `ajl` (AWS JSON Line). This guide covers getting a
dev environment running, the workflows you'll actually use, and what a good
change looks like. For code conventions see [STYLE_GUIDE.md](STYLE_GUIDE.md);
for the reasoning behind the architecture see [DESIGN.md](DESIGN.md).

## Getting set up

Requirements: Python ≥ 3.10, [uv](https://github.com/astral-sh/uv), and for
model tooling `jq`/`yq`/`git`. If you use [mise](https://mise.jdx.dev/), the
repo's `mise.toml` pins `aws`, `uv`, `jq` and `yq` — just run `mise install`.

```shell
git clone <repo-url> ajl
cd ajl
uv sync            # creates .venv with runtime + dev dependencies
uv run pytest      # everything should pass before you start
```

Run the CLI from source:

```shell
uv run ajl s3 list-buckets
uv run ajl ec2 describe-instances --verbose
```

Real AWS credentials (a profile or environment variables) are needed to
exercise the CLI end-to-end; the test suite runs entirely offline with fakes.

## Repo layout

| Path | What it is |
|---|---|
| `src/ajl/main.py` | CLI entry point, arg parsing, param coercion, worker pool |
| `src/ajl/normalize.py` | Generic Type/Id/Name/Arn/Tags normalizer |
| `src/ajl/pagination.py` | botocore paginators + marker-loop fallback |
| `src/ajl/tags.py` | `--fetch-tags` batching via the Resource Groups Tagging API |
| `src/ajl/modelconfig.py` | Loads packaged service models (`AJL_MODELS_DIR` overrides) |
| `src/ajl/models/*.json` | Generated per-service models + curated output shaping |
| `tools/` | Model generation and curation scripts (see `tools/README.md`) |
| `tests/` | Offline pytest suite |
| `alpha-scripts/` | Early experiments kept for reference — not part of the package |

## The most common contribution: improving a service's output

The output shaping for every service lives in model files, and the curated
part of those files is **generated** — do not edit `src/ajl/models/*.json`
shaping by hand, it will be wiped by the next regeneration.

1. Add or update the `output.resources` config (or, only if the declarative
   config can't express the shape, an `output.jq` program) in
   `tools/apply-resource-configs.py`. Prefer the declarative config; jq is
   the escape hatch and wins over the config when both exist.
2. Re-apply it: `python3 tools/apply-resource-configs.py`
3. Test against a live account, pointing at your working tree's models if
   you haven't reinstalled: `AJL_MODELS_DIR=src/ajl/models uv run ajl <service> <operation>`
4. Check the contract: every record should lead with sensible `Type`, `Id`,
   `Name`, `Arn`, `Tags` values. `Arn` should be real (verify the
   `arn_format` against the [ARN reference](https://docs.aws.amazon.com/service-authorization/latest/reference/reference_policies_actions-resources-contextkeys.html)),
   `Tags` should be a map, and ideally the record should pipe cleanly back
   into a related operation via `--params-json -`.

### Adding a brand-new service

```shell
uv run python tools/generate-model.py <service> [...]   # e.g. cloudwatch
python3 tools/apply-resource-configs.py
```

`<service>` is the boto3 client name. Then curate the important list/describe
operations as above. Unconfigured operations still work — they fall back to
heuristic unwrapping — so it's fine to curate incrementally, but a new model
should at least cover the service's main `list`/`describe` calls.

### Regenerating all models (e.g. after a boto3 upgrade)

```shell
uv run python tools/generate-model.py --all-existing
python3 tools/apply-resource-configs.py    # always — regeneration wipes curation
```

## Tests

```shell
uv run pytest
uv run pytest tests/test_normalize.py -k arn   # narrow while iterating
```

The suite covers the normalizer, param coercion, pagination and tag batching
using in-memory fakes — no AWS calls, no credentials. Changes to those areas
need tests; new curated shaping is best verified with a live smoke test
(paste the command and a sample output line in your PR description).

## Code changes

- Run `uv run ruff check src tests tools` and `uv run ruff format` before
  committing.
- Keep stdout sacred: only JSONL records may be written to stdout.
  Everything else — logs, warnings, per-request errors — goes to stderr,
  prefixed `ajl:`.
- A failed request in fan-out mode must not kill the stream; report to
  stderr, count it, exit non-zero at the end.
- Backwards compatibility of the output contract matters most: the leading
  `Type`/`Id`/`Name`/`Arn`/`Tags` properties and the pipe-back-in behavior of
  `--params-json` are the public API. If a change alters either, record the
  decision in [DESIGN.md](DESIGN.md).

## Commits and PRs

Commit messages follow conventional-commit prefixes, matching the existing
history: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`. Subject in
the imperative, body explaining *why* when it isn't obvious.

A good PR:

- does one thing (one service curated, one bug fixed);
- includes tests for Python changes, or a live smoke-test transcript for
  model curation;
- updates `README.md` / `DESIGN.md` when behavior or a design decision
  changes.

## Releasing

The version is derived from git tags by setuptools-scm — there is no version
field to bump. Add a [CHANGELOG.md](CHANGELOG.md) entry for the release,
then tag the release commit and build:

```shell
git tag v0.3.0
git push origin v0.3.0
uv build                                      # produces ajl-0.3.0
uv publish --index testpypi --token <token>   # TestPyPI first
```

Untagged commits build as dev versions (`0.3.1.dev2+g<sha>`), so anything
without a clean `vX.Y.Z` tag is visibly not a release.

Verify the published package in a scratch venv
(`uv pip install -i https://test.pypi.org/simple/ ajl`) before any wider
release.