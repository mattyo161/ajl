# ajl Style Guide

Conventions for code in this repo. When in doubt, match the surrounding code
— these rules describe what the codebase already does.

## Python

- Target Python ≥ 3.10 (`pyproject.toml` is the source of truth).
- Lint and format with **ruff**: `uv run ruff check src tests tools` and
  `uv run ruff format`. Don't hand-fight the formatter.
- 4-space indent, double quotes, ~100 character lines.
- `snake_case` functions/variables, `PascalCase` classes, `UPPER_CASE` module
  constants. Private helpers get a leading underscore (`_marker_pairs`,
  `_id_from_arn`).
- Imports in three groups — stdlib, third-party, local (`from .modelconfig
  import ...`) — each alphabetized. Relative imports inside the package.
- Type hints are used sparingly, where they clarify a public signature
  (`load_service_model(service: str)`). Don't blanket-annotate.

## Docstrings and comments

- Every module starts with a docstring that explains the module's **design**,
  not just its contents — see `normalize.py` and `tags.py` for the model:
  what the module guarantees, the config schema it consumes, the trade-off it
  makes. This is where architectural documentation lives; keep it current
  when behavior changes.
- Public functions get a one-line docstring stating what they return or
  guarantee ("Yield response pages for an operation, following pagination.").
- Inline comments state constraints the code can't express — API limits,
  AWS quirks ("ecs-style tag lists use lowercase key/value"), lock ownership
  ("caller holds self.lock"). Never narrate what the next line does.

## AWS field naming

Two casing worlds meet in this codebase; keep them separate:

- **PascalCase** for everything that faces the AWS API or the output stream:
  request params after coercion, response fields, and the normalized
  `Type`/`Id`/`Name`/`Arn`/`Tags` properties.
- **kebab-case** on the CLI (`--max-items`, `describe-instances`),
  **snake_case** for boto3 method names. Conversions go through
  `caseconverter` (`pascalcase`/`snakecase`), never hand-rolled.

## Output and error conventions

- **stdout is JSONL only.** One record per line, serialized with the shared
  `dumps()` (orjson, `default=str`). All writes go through an `Emitter` so
  concurrent workers can't interleave lines.
- Everything human-facing goes to **stderr**, prefixed `ajl:`
  (`print(f"ajl: ...", file=sys.stderr)`).
- Errors are contained, not fatal: catch per-request, report to stderr, keep
  streaming, return exit code 1 at the end. Reserve raising for programmer
  errors.
- Missing normalized values are empty (`""`, `{}`) rather than absent or
  `null`, so downstream jq never needs `// empty` guards on the contract
  properties.

## Concurrency

- Threads only (the workload is I/O-bound). Shared mutable state — client
  caches, buffers, counters — is guarded by an explicit `threading.Lock`,
  held for as short a span as possible.
- Never call the network while holding a lock (see `Runner.account` for the
  pattern: check cache under the lock, fetch outside it, store under the
  lock).

## Model files and curation

- `src/ajl/models/*.json` is generated output. The durable home of curated
  shaping is `tools/apply-resource-configs.py` — edit there and re-apply.
- Prefer declarative `output.resources` configs; write `output.jq` only when
  the config can't express the shape. jq programs follow the existing style:
  a leading comment per logical step, `$r`/`$reservation`-style bindings for
  root context, and `//""` fallbacks so missing fields never emit `null`.
- `arn_format` templates must produce real ARNs — check against the AWS
  service authorization reference before committing.

## Tests

- pytest, offline, no AWS credentials: fake clients/pages as plain dicts and
  small stub classes.
- Test files mirror module names (`tests/test_normalize.py` ↔
  `src/ajl/normalize.py`). Test names say the behavior:
  `test_name_falls_back_to_tags_name`, not `test_normalize_2`.
- Cover fallback paths (missing Arn, empty tags, scalar list items), not
  just the happy path — the fallbacks *are* the contract.

## Shell scripts (tools/)

- `bash` with the safety net where fitting; long `jq` programs go in single
  quotes with `#` comments explaining each non-obvious step (see
  `tools/README.md` for the house style).
- Scripts must be runnable from any directory — resolve paths relative to
  the script file, as `apply-resource-configs.py` does.

## Commit messages

Conventional-commit prefixes: `feat:`, `fix:`, `docs:`, `test:`,
`refactor:`, `chore:`. Imperative subject, lowercase after the prefix, body
for the *why*.