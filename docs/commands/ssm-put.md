# `ajl ssm put`

Create a new SSM parameter, or overwrite an existing one with an explicit
type/key/tier — the opposite of `ssm update`'s "preserve what's already
there." Source: `src/ajl/ssm.py` (`build_write_parser`, `run_write`, shared
with `ssm update`).

## Flags

| Flag | Default | Notes |
|---|---|---|
| `--name` | — | required (single mode) |
| `--value` | — | required (single mode); `-` reads stdin |
| `--type` | `String` | `String` \| `StringList` \| `SecureString` |
| `--key-id` | — | KMS key for `SecureString`; omit to use the AWS-managed key |
| `--tier` | — | `Standard` \| `Advanced` \| `Intelligent-Tiering` |
| `--description` | — | |
| `--overwrite` | off | without it, `PutParameter` fails if the name already exists |

## Single vs. bulk

```shell
ajl ssm put --name /app/feature-flag --value enabled
ajl ssm put --name /app/db/password --value - --type SecureString --key-id alias/app <<< "s3cr3t"
```

Bulk streams records via `--params-json -`; each line's own `Type`/
`Overwrite` (either casing) override the CLI defaults for just that item —
useful for a batch with a mix of types:

```shell
printf '{"Name":"/a","Value":"1"}\n{"Name":"/b","Value":"2","Type":"SecureString"}\n' \
  | ajl ssm put --params-json - --overwrite
```

Every write emits:

```json
{"Type": "ssm:parameter", "Name": "/app/feature-flag", "Action": "put", "Version": 1, "Tier": "Standard"}
```

`Action` is always `"put"` here (unlike `update`, which distinguishes
`"updated"` from `"unchanged"` — `put` doesn't compare against an existing
value, it just writes).

A `Value` arriving as a sealed `AJLSEC:1:...` envelope is automatically
unsealed before writing, same as `ssm update` — see
[ssm-get.md](ssm-get.md) and [decrypt.md](decrypt.md).

## Errors are contained, not fatal

A bad value or an API error for one item (e.g. `--overwrite` not set and
the name already exists) is reported to stderr and counted; the rest of
the batch keeps going. Exit code `1` if any item errored, `0` otherwise.

## See also

- [ssm-update.md](ssm-update.md) — update an existing parameter's value only, preserving its type/key/tier
- [ssm-get.md](ssm-get.md) — the read side
