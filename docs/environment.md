# Environment variables

Every environment variable ajl reads, what it does, and how it relates to
the others. Most have a matching CLI flag; the flag always wins for that
one invocation, the env var sets the default for every invocation in the
shell. Source of truth is the code — each row names the file that reads it.

## AWS identity — which account/region

| Variable | Read by | Effect |
|---|---|---|
| `AWS_PROFILE` | `main.py` | Default `--profile`. |
| `AWS_DEFAULT_PROFILE` | `main.py` | Fallback if `AWS_PROFILE` is unset. |
| `AWS_REGION` | `main.py` | Default `--region`. |
| `AWS_DEFAULT_REGION` | `main.py` | Fallback if `AWS_REGION` is unset; if both are unset the built-in default is `us-east-1`. |
| `AWS_CONFIG_FILE` | `fanout.py` | Where `--all-profiles`/`--all` reads named profiles from (default `~/.aws/config`). |

These are the same variables the AWS CLI and every boto3 tool read — no
ajl-specific naming here on purpose, so a shell already set up for `aws`
just works.

## Fan-out — `--all` / `--all-profiles` / `--all-regions`

| Variable | Read by | Effect |
|---|---|---|
| `AJL_PROFILES` | `fanout.py` | Comma/space list of profile names. When set, this *replaces* discovery from `~/.aws/config` as the source list for `--all-profiles`/`--all` — but that list still goes through the same account-dedup as config-discovered profiles (below). It changes *which profiles are candidates*, not whether dedup runs. |
| `AJL_REGIONS` | `fanout.py` | Comma/space list of regions. When set, this replaces botocore's static per-service region list for `--all-regions`/`--all`, and — because you named them — opt-in regions are included even though they'd otherwise be excluded by default. |

Relationship worth knowing: `--all-profiles`/`--all` always resolves every
candidate profile's account id in parallel and **de-dupes by account** — if
two profiles map to the same account, only the first one found wins
(arbitrarily; there's no preference order yet), whether that candidate list
came from `~/.aws/config` or from `AJL_PROFILES`. The only way to skip dedup
entirely is the CLI `--profiles` flag (not an env var) — explicitly named
profiles are taken verbatim, in the order given, no dedup, no opt-in-region
filtering either: "you named them, you meant them" (`fanout.py`). If dedup
keeps picking the wrong profile for an account (e.g. a limited role instead
of your usual one), `--profiles a b c` is the workaround, not `AJL_PROFILES`.

## Client behavior

| Variable | Read by | Effect |
|---|---|---|
| `AJL_CONNECT_TIMEOUT` | `main.py` (`Runner.client`) | botocore connect timeout in seconds (default `5`). Low on purpose: an unreachable endpoint (a disabled opt-in region) fails in seconds during fan-out instead of hanging on botocore's 60s default. |
| `AJL_READ_TIMEOUT` | `main.py` (`Runner.client`) | botocore read timeout in seconds (default `30`). |
| `AJL_MODELS_DIR` | `modelconfig.py` | Directory to load `<service>.json` model files from instead of the packaged ones. Point this at `src/ajl/models` when developing so curation changes are visible without reinstalling — every example in these docs that says `AJL_MODELS_DIR=src/ajl/models` is doing exactly that. |
| `AJL_DEBUG_CACHE` | `debug.py` | `1` prints a stderr note every time an *internal* cache is hit — boto3 sessions/clients, resolved account ids, compiled jq programs, service models, scan's s3 clients. For debugging "why did this rebuild a client" surprises, not the result cache below. |

## The result cache — `--cache` / `AJL_CACHE`

See [data-files.md](data-files.md) for the on-disk layout this produces.

| Variable | Read by | Effect |
|---|---|---|
| `AJL_CACHE` | `cache.py` | Default TTL (e.g. `15m`, `2h`) when `--cache` isn't passed. `--no-cache` disables caching for one run even when this is set. |
| `AJL_CACHE_DIR` | `cache.py` | Cache directory (default `${XDG_CACHE_HOME:-~/.cache}/ajl`). |
| `AJL_CACHE_RM_AFTER` | `cache.py` | Default entry lifetime (default `7d`) before an opportunistic sweep deletes it; `--rm-after` overrides per run. |

## age encryption — shared by the cache and by field-level sealing

One key configuration covers **two independent protections** (see
[DESIGN.md](../DESIGN.md) "Two orthogonal secret protections"): the whole
cache file at rest (`cache.py`), and individual `SecureString` *values*
inline in the JSONL stream (`seal.py`, `ajl ssm get` — see
[commands/ssm-get.md](commands/ssm-get.md)). Both read the same three
variables, so one identity secures both.

| Variable | Read by | Effect |
|---|---|---|
| `AJL_AGE_IDENTITY` | `cache.py` (`_load_identity`) | An `AGE-SECRET-KEY-...` string, **or** a path to a file containing one (the file form is checked first if the value doesn't start with `AGE-SECRET-KEY-`). Enables both encrypt and decrypt — the recipient is derived from the identity. `ajl cache keygen` generates one (see [commands/cache.md](commands/cache.md)). |
| `AJL_AGE_RECIPIENTS` | `cache.py` (`_load_recipients`) | Comma/space list of `age1...` public keys. Write-only: encrypts, but nothing here can decrypt without also having `AJL_AGE_IDENTITY`. Use this on a machine that should be able to write secrets to the cache but never read them back. |
| `AJL_AGE_PASSPHRASE` | `cache.py` | A shared passphrase, symmetric mode (same value encrypts and decrypts). Simpler than a keypair for solo/local use; doesn't scale to "many people, one shared secret store" the way `AJL_AGE_IDENTITY` + `AJL_AGE_RECIPIENTS` does. |

Precedence when more than one is set (`cache.py`'s `encryption_mode()`):
**`AJL_AGE_RECIPIENTS`/`AJL_AGE_IDENTITY` (mode `"age"`) win over
`AJL_AGE_PASSPHRASE` (mode `"passphrase"`)** — passphrase mode only
activates when neither of the other two is set. Decrypting always tries
passphrase mode first if `AJL_AGE_PASSPHRASE` is set, falling back to the
identity — so it's possible (if unusual) to have both configured and have
ajl try both on read.

`ssm`/`secretsmanager` + `--cache` **refuses to run** unless
`encryption_mode()` is non-`None` — caching a secret unencrypted isn't an
option that exists.

## The learn log — `--learn` / `AJL_LEARN`

| Variable | Read by | Effect |
|---|---|---|
| `AJL_LEARN` | `learn.py` | `1`/`true`/`yes`/`on` enables `--learn` behavior for every invocation. `--no-learn` disables it for one run even when this is set. |
| `AJL_LEARN_FILE` | `learn.py` | Where the JSONL audit record is appended (default `${XDG_STATE_HOME:-~/.local/state}/ajl/learn.jsonl`). |

## The per-API-call telemetry log — `--api-log` / `AJL_APILOG`

| Variable | Read by | Effect |
|---|---|---|
| `AJL_APILOG` | `apilog.py` | `1`/`true`/`yes`/`on` enables `--api-log` behavior for every invocation. `--no-api-log` disables it for one run even when this is set. |
| `AJL_APILOG_FILE` | `apilog.py` | Where the JSONL record is appended (default `${XDG_STATE_HOME:-~/.local/state}/ajl/apilog.jsonl`). |

`AJL_LEARN_FILE`/`AJL_APILOG_FILE` both default under the *same*
`XDG_STATE_HOME` root but are two **separate files** (`learn.jsonl` /
`apilog.jsonl`) — one line per *invocation* (learn) vs. one line per
*underlying botocore call* (api-log), so a single `ajl` command with
pagination or fan-out produces one learn-log line and potentially many
api-log lines.

## XDG base directories

ajl follows the [XDG base directory
spec](https://specifications.freedesktop.org/basedir-spec/) for its own
state, with ajl-specific overrides available at each layer:

| Purpose | XDG variable | ajl override | Default |
|---|---|---|---|
| Cache (`--cache` results) | `XDG_CACHE_HOME` | `AJL_CACHE_DIR` | `~/.cache/ajl` |
| State (`learn.jsonl`, `apilog.jsonl`) | `XDG_STATE_HOME` | `AJL_LEARN_FILE` / `AJL_APILOG_FILE` (whole file path, not just a directory) | `~/.local/state/ajl/` |

## Precedence summary

For every setting above the resolution order is the same, narrowest wins:

1. A CLI flag for this one invocation (`--cache`, `--profile`, `--no-learn`, ...)
2. The matching env var (applies to every invocation in the shell)
3. The built-in default named in the tables above

## See also

- [data-files.md](data-files.md) — what actually gets written to disk and where
- [DESIGN.md](../DESIGN.md) — why the cache and sealing share one age config
- [request-flow.md](request-flow.md) — where in the request lifecycle each of these gets read
