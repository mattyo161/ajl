# Agent Guide for ajl

`ajl` (AWS JSON Line) is a Python CLI that wraps boto3, streams AWS API
responses as JSONL, and normalizes every resource to lead with
`Type`, `Id`, `Name`, `Arn`, `Tags`. Records from one invocation can be piped
back into another via `--params-json -` for massive parallel fan-out.

Read [DESIGN.md](DESIGN.md) before changing architecture,
[STYLE_GUIDE.md](STYLE_GUIDE.md) before writing code, and
[CONTRIBUTING.md](CONTRIBUTING.md) for workflows. This file is the condensed
operational version.

## Commands

```shell
uv sync                                    # install deps into .venv
uv run pytest                              # full test suite (offline, no AWS creds)
uv run pytest tests/test_normalize.py -k x # narrow run
uv run ruff check src tests tools          # lint
uv run ruff format src tests tools         # format
uv run ajl s3 list-buckets                 # run the CLI from source (needs AWS creds)
uv run python tools/generate-model.py <service>   # generate a service model
python3 tools/apply-resource-configs.py    # re-apply curated shaping (ALWAYS after generate)
uv build                                   # build sdist+wheel into dist/
```

Use `uv run ...` for anything needing the venv. Live CLI runs need real AWS
credentials; the test suite does not.

## Map

- `src/ajl/main.py` — entry point: arg parsing (`--` params are kebab-case →
  PascalCase, coerced from model input types), `Runner` (boto3 session/client
  cache per profile+region), `Emitter` (line-atomic stdout), `--params-json`
  worker-pool fan-out.
- `src/ajl/normalize.py` — the generic normalizer driven by declarative
  `output.resources` configs; module docstring documents the config schema.
- `src/ajl/pagination.py` — botocore paginators first, marker-loop fallback
  from model `input.markers`/`output.markers`.
- `src/ajl/tags.py` — `--fetch-tags` batching (100 ARNs/call) via the
  Resource Groups Tagging API, background threads, submission-order emit.
- `src/ajl/modelconfig.py` — loads packaged models; `AJL_MODELS_DIR` env var
  overrides for development.
- `src/ajl/models/*.json` — per-service models. **Generated files.**
- `tools/apply-resource-configs.py` — the real home of curated output
  shaping (declarative configs + hand-written jq programs).
- `tests/` — pytest with in-memory fakes.
- `alpha-scripts/`, `.temp/`, `models/` (repo root), `*.jsonl` at root —
  experiments and scratch data. Do not build on them.

## Hard rules

1. **Never hand-edit shaping in `src/ajl/models/*.json`.** Curated
   `output.resources` / `output.jq` live in `tools/apply-resource-configs.py`;
   edit there, then run it. Direct edits are silently lost on the next
   `generate-model.py` run.
2. **stdout is JSONL only.** Any diagnostic output goes to stderr prefixed
   `ajl:`. Never `print()` to stdout outside the `Emitter`.
3. **Don't break the output contract**: leading `Type`/`Id`/`Name`/`Arn`/`Tags`
   on every shaped record, `Tags` always a map, missing values as `""`/`{}`
   (not null/absent), records re-pipeable via `--params-json`. If a change
   touches this, add an entry to DESIGN.md.
4. **Per-request errors must not kill a stream.** Catch, report to stderr,
   continue, exit 1 at the end.
5. **Locks are held briefly and never across network calls** (see
   `Runner.account` for the check/fetch/store pattern).
6. Prefer declarative `output.resources` configs over `output.jq`; jq is the
   escape hatch for shapes the config can't express (and wins when both are
   set).

## Typical tasks

**Curate a service's output** — edit `tools/apply-resource-configs.py`
(add `r(path, type, id_, name, arn/arn_format, tags, scalar_as)` entries or a
jq program), run it, smoke-test with
`AJL_MODELS_DIR=src/ajl/models uv run ajl <service> <op>`, verify the five
leading properties and that the ARN format is real.

**Add a service** — `uv run python tools/generate-model.py <boto3-name>`,
then apply-resource-configs, then curate the main list/describe operations.

**Change normalizer/pagination/tags/coercion** — add or update tests in the
matching `tests/test_*.py`; the suite is offline, so fake pages as dicts.

**Upgrade boto3** — regenerate all models (`--all-existing`) and re-apply
configs in the same change.

## Verification checklist

- `uv run pytest` passes.
- `uv run ruff check src tests tools` is clean.
- For output-affecting changes: paste a sample emitted line and check the
  contract properties by eye.
- Commit style: `feat:`/`fix:`/`docs:`/`test:`/`refactor:`/`chore:` +
  imperative subject.