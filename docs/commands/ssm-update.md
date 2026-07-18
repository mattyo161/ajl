# `ajl ssm update`

Update an *existing* SSM parameter's value, preserving its `Type`/`KeyId`/
`Tier` — skip-if-unchanged by default. Source: `src/ajl/ssm.py`
(`build_write_parser`, `run_write`, shared with `ssm put`).

## What makes this different from `ssm put`

`update` looks the parameter up first (`describe_parameters` filtered by
name) and **refuses to run if it doesn't exist** — `"{name} does not exist —
use \`ssm put\` to create it"`. It then reuses that parameter's existing
`Type`, `KeyId` (if `SecureString`), and `Tier` — you supply a name and a
value, nothing about how the parameter is configured.

## Skip-if-unchanged

Unless `--force`, `update` fetches the current (decrypted) value and
compares it to the one you're writing. Identical → no `PutParameter` call
at all, and the emitted record says so:

```json
{"Type": "ssm:parameter", "Name": "/app/db/host", "Action": "unchanged", "Version": 3}
```

A real write emits `"Action": "updated"` with the new `Version` and `Tier`.
This matters at scale — a bulk update of parameters where most values
haven't actually changed does far fewer API calls than a naive "just put
everything" script would.

## Single vs. bulk

```shell
ajl ssm update --name /app/db/host --value db.prod.internal
echo "new-password" | ajl ssm update --name /app/db/password --value -   # value from stdin
```

Bulk streams `{Name, Value}` records (either casing) via `--params-json -`,
fanned across `--workers`:

```shell
ajl ssm get --path /app --recursive --decrypt \
  | jq -c 'select(.Name | endswith("/host")) | {Name, Value: "new-value"}' \
  | ajl ssm update --params-json -
```

A `Value` that arrives as a sealed `AJLSEC:1:...` envelope (piped straight
back from `ssm get`'s bulk output, no `--decrypt` needed on that side) is
automatically unsealed before writing — the same `AJL_AGE_*` configuration
that sealed it is required to unseal it (see [ssm-get.md](ssm-get.md),
[decrypt.md](decrypt.md)).

## Errors are contained, not fatal

A missing parameter, a bad value, or an API error for one item is reported
to stderr and counted — the rest of the batch keeps going. Exit code `1` if
any item errored, `0` otherwise.

## See also

- [ssm-put.md](ssm-put.md) — create parameters, or force-overwrite with explicit type/key/tier
- [ssm-get.md](ssm-get.md) — the read side; a natural source for `update`'s `--params-json` input
