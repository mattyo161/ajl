# `ajl decrypt`

A standalone filter: read JSONL from stdin, unseal every `AJLSEC:1:...`
envelope found anywhere in it, write the result to stdout. For piping in
data that was sealed by ajl but is arriving from somewhere else — a
restored backup, another tool, a file someone handed you — not just ajl's
own live output. A pseudo-command like `ajl cache`, dispatched before any
service/operation parsing (`service == "decrypt"`). Source:
`src/ajl/ssm.py` (`run_decrypt_filter`), `src/ajl/seal.py`
(`unseal_obj`).

## Why a command and not a `--decrypt` global flag

`ssm get` already has its own `--decrypt` flag with its own meaning
(force plaintext output instead of sealing) — a global `--decrypt` flag
would collide with it. `ajl decrypt` is a separate pseudo-command
specifically to avoid that clash.

## What it does

Walks every value in every JSON object on stdin, recursively — not just
top-level fields — and unseals anything shaped like `AJLSEC:1:<base64>`.
Anything else passes through untouched:

```shell
restored-secrets.jsonl | ajl decrypt
```

Requires the same `AJL_AGE_*` configuration that sealed the values in the
first place (see [../environment.md](../environment.md)). Unlike the result
cache — where an entry the current key can't open is just treated as a
miss and the command re-runs — `ajl decrypt` has no fallback: a value it
can't decrypt (wrong or missing identity) raises uncaught, stopping the
filter with a traceback rather than a clean `ajl: ...` message. Point it at
the right identity before piping a large file through it; there's currently
no per-line error containment here the way there is for, say, `ssm get`'s
per-name failures.

## Examples

```shell
ajl ssm get --path /app --recursive | ajl decrypt              # unseal a bulk get's output
cat old-backup.jsonl | ajl decrypt > plaintext.jsonl            # a file from elsewhere
```

## See also

- [ssm-get.md](ssm-get.md) — the most common source of sealed values
- [cache.md](cache.md) — `ajl cache keygen`, the source of the `AJL_AGE_IDENTITY` this depends on
- [../environment.md](../environment.md) — the `AJL_AGE_*` variables in full
