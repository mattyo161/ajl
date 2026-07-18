# `ajl cache`

Manage the result cache (`--cache TTL` — see [../data-files.md](../data-files.md)
for the on-disk layout this manages). A pseudo-command, dispatched like
`ajl decrypt` before any service/operation parsing happens — `ajl cache
<keygen|ls|clear>` is not `<service> <operation>`. Source: `src/ajl/cache.py`
(`run_cache_command`).

## `ajl cache keygen`

Generates an age (x25519) keypair and prints it — nothing is written to a
file automatically:

```
$ ajl cache keygen
# public key: age1dy6cmpryqhap55h8vuusp6lkfe6qhmfua4p4cceetcv6rgqkdaxsd20sml
AGE-SECRET-KEY-1PE7PFR3KSSYAGL6GEZE5VK8XNETE4MWLF600WTLT960G49N3N8PQUJUNUZ
ajl: export AJL_AGE_IDENTITY='AGE-SECRET-KEY-...' to encrypt+decrypt the result cache with this key
```

Both lines are stdout (the reminder below them is stderr). Line 1 is the
**public** key — the value to hand someone else for `AJL_AGE_RECIPIENTS`
(write-only: they can seal values for you, they can't read anything back).
Line 2 is the **secret** identity — `AJL_AGE_IDENTITY`, which can both seal
and unseal. `ajl cache keygen | tail -1` grabs exactly the secret line, the
one you'd actually export. This is the exact form every age-identity guard
message in ajl points you at (see [ssm-get.md](ssm-get.md)):

```shell
export AJL_AGE_IDENTITY=$(ajl cache keygen | tail -1)   # this session only
```

Add that line to a shell profile to persist it — `keygen` itself never
touches disk, so persistence is entirely your call.

## `ajl cache ls`

One JSONL line per cache entry, read straight from each `<key>.meta.json`
(no decryption, no reading the actual gzipped body):

```json
{"Key": "3f9a2b1c...", "Command": "ajl ecs list-clusters --all-regions",
 "Age": "12m", "ExpiresIn": "6.9d", "Lines": 37, "Bytes": 4821,
 "Encrypted": true, "DurationS": 4.2}
```

`Bytes` is the actual file size on disk (gzipped, and age-sealed on top if
`Encrypted`). `Age`/`ExpiresIn` are computed relative to *now*, not stored
verbatim — running `ls` again later shows them ticking.

## `ajl cache clear [--all]`

Without `--all`: sweeps only *expired* entries (the same sweep every
cache-enabled invocation does opportunistically — this just lets you run it
on demand). With `--all`: removes every entry regardless of expiry.
Reports how many files were removed to stderr; never touches entries for
another key's TTL that hasn't elapsed unless `--all` is given.

```shell
ajl cache clear          # tidy up — only what's already stale
ajl cache clear --all    # nuke the whole cache
```

## See also

- [../data-files.md](../data-files.md) — the `.meta.json`/`.jsonl.gz`/`.jsonl.gz.age` files these commands manage
- [../environment.md](../environment.md) — `AJL_CACHE_DIR`, `AJL_AGE_*`
- [decrypt.md](decrypt.md) — the other half of the age configuration: unsealing values, not decrypting the cache
