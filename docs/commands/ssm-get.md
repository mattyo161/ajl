# `ajl ssm get`

Fetch SSM parameters, picking the right underlying API from which flag you
gave it — the way you'd wish `aws ssm` did. A custom command (like `s3
scan`/`s3 list`), not the generic `<service> <operation>` path, because its
record shape and sealing defaults need logic the generic normalizer can't
express. Source: `src/ajl/ssm.py` (`build_get_parser`, `run_get`).

## Picking the API

Exactly one of these is required:

| Flag | API called | Shape |
|---|---|---|
| `--name X` | `get_parameter` | one parameter |
| `--names A B C ...` | `get_parameters` | chunked 10/call (the API's hard max), fanned across `--workers` |
| `--path /some/path [--recursive]` | `get_parameters_by_path` | paginated |

`--names` also reads newline-delimited names from stdin — either
`--names -` explicitly, or bare `--names` with nothing piped through a TTY.
That's how a million keys stream through the worker pool ten at a time:

```shell
cat names.txt | ajl ssm get --names -
```

Decryption is **on by default** for all three forms; `--no-decryption` sets
`WithDecryption=false`. That flag never reaches the API as a literal
parameter — SSM has no such parameter — it only controls this one boolean.

## Output shape

```json
{"Name": "...", "Value": "...", "Type": "String", "Version": 3,
 "LastModifiedDate": "...", "DataType": "text",
 "ajl": {"type": "ssm:parameter", "id": "...", "name": "...", "arn": "...", "tags": {}}}
```

`Type` here is the parameter's own `String`/`StringList`/`SecureString`
value, straight from the API — no rename needed since it can't collide
with `ajl.type` anymore. `ajl.arn` falls back to `""` if the API response
has none; `ajl.tags` is always `{}` (SSM tags aren't fetched here).

### Single value vs. full record

|  | default | force the other |
|---|---|---|
| `--name` (single) | bare value, one line, no JSON wrapper | `--json` for the full record |
| `--names`/`--path` (bulk) | full JSON record | `--raw` for bare values, one per line |

The intuition: asking for exactly one thing, you want that one value; asking
for many, you want structure you can `jq` — but either can be forced the
other way.

## SecureString sealing

Bulk output (`--names`/`--path`) seals `SecureString` values by default —
`--raw` output is the exception, since there's no JSON envelope to put a
sealed value in. A single `--name` is plaintext by default (you asked for
one specific thing) unless `--encrypt` forces sealing. `--decrypt` always
wins over both — forces plaintext regardless of single/bulk.

|  | single `--name` | bulk `--names`/`--path` |
|---|---|---|
| default | plaintext | sealed (unless `--raw`) |
| force sealed | `--encrypt` | *(already sealed)* |
| force plaintext | *(already plaintext)* | `--decrypt` |

Sealing needs an age recipient configured (`AJL_AGE_IDENTITY` /
`AJL_AGE_RECIPIENTS` / `AJL_AGE_PASSPHRASE` — see
[../environment.md](../environment.md)); without one, a bulk `get` that
would otherwise seal refuses to run and tells you exactly how to fix it:

```
ajl: ssm get seals SecureString values by default — no age identity configured
ajl: set one up: export AJL_AGE_IDENTITY=$(ajl cache keygen | tail -1) — add that to your shell profile to persist it (or use AJL_AGE_RECIPIENTS/AJL_AGE_PASSPHRASE instead)
ajl: or skip sealing for this run: pass --decrypt for plaintext output
```

A sealed value round-trips through anything — a file, `--params-json`, the
result cache — as `AJLSEC:1:<base64>`, and only comes back to plaintext
through `ajl decrypt` or `ssm get --decrypt` with the same age
configuration (see [decrypt.md](decrypt.md)).

## Errors are contained, not fatal

A name that doesn't exist (or you lack access to) is reported to stderr and
counted, not fatal to the run — the rest of the batch still streams. Exit
code is `1` if any name was invalid/inaccessible, `0` otherwise (a chunk
that fails outright — e.g. throttled past retry — also exits `1`).

## Examples

```shell
ajl ssm get --name /app/db/host                  # one value, plaintext, bare
ajl ssm get --name /app/db/password --encrypt     # one value, sealed
ajl ssm get --names /a /b /c                      # bulk, sealed JSON records
ajl ssm get --names /a /b /c --decrypt            # bulk, plaintext JSON records
ajl ssm get --path /app --recursive               # a whole hierarchy, sealed
cat names.txt | ajl ssm get --names - --raw --decrypt | wc -l   # just count them
```

## See also

- [ssm-params.md](ssm-params.md) — `ssm params`, the `describe-parameters` alias (metadata, not values)
- [ssm-update.md](ssm-update.md) / [ssm-put.md](ssm-put.md) — the write side
- [decrypt.md](decrypt.md) — unsealing `AJLSEC:` values from any source
- [../environment.md](../environment.md) — the `AJL_AGE_*` variables sealing depends on
