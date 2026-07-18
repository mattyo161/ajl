# `ajl ssm params`

A thin alias for `aws ssm describe-parameters` — **metadata about
parameters** (name, type, tier, last-modified, policies), never a `Value`.
For values, see [ssm-get.md](ssm-get.md); this command exists because
`describe-parameters` is the natural way to *discover* names/filter by tag
before fetching values, and because it's throttled hard enough on real
accounts to be worth its own doc note. Source: dispatched directly in
`main.py` (`service == "ssm" and operation == "params"`), not a custom
module — it runs through the same generic path as any `<service>
<operation>` call.

## Why not just `ajl ssm describe-parameters`?

You can — `params` is sugar for exactly that, with two things layered on
that the generic path wouldn't give you by name alone:

- **Adaptive retries** — every `Runner` client already uses botocore's
  adaptive retry mode, but `describe-parameters` is the API this was
  written for: it throttles more aggressively than almost anything else in
  SSM on a real account with many parameters.
- **`--cache` fits naturally** — the full parameter list rarely changes
  minute to minute, so a slow `describe-parameters` scan becomes a one-time
  cost per TTL window: `ajl ssm params --cache 1h`.

## Output shape

The standard contract, unlike `ssm get`'s custom narrower shape — `Id` is
the parameter name, `Tags` is `{}` (the API doesn't return tags on this
call):

```json
{"Type": "ssm:parameter", "Id": "/app/db/host", "Name": "/app/db/host",
 "Arn": "arn:aws:ssm:us-east-1:123456789012:parameter/app/db/host", "Tags": {},
 "OriginalType": "String", "Version": 3, "LastModifiedDate": "...", "Tier": "Standard"}
```

SSM's own response also has a field called `Type` — the parameter's
`String`/`StringList`/`SecureString` — which collides with ajl's own `Type`
contract property. The generic normalizer (`normalize_resource` in
`normalize.py`) handles this generically for every service, not just here:
a raw field whose name collides with a contract property, and whose value
actually differs, is kept under `Original<Key>` instead of being dropped —
so nothing is silently lost, it's just renamed. This is the same collision
`ssm get`'s custom `_shape()` sidesteps on purpose by calling it
`ParameterType` from the start (see [ssm-get.md](ssm-get.md)); `params`
runs through the generic path and gets the generic `Original*` treatment
instead.

## Filtering and fan-out

Any real `describe-parameters` parameter works as a normal `--kebab-flag`:

```shell
ajl ssm params --parameter-filters '[{"Key":"Name","Option":"BeginsWith","Values":["/app/"]}]' --cache 1h
ajl ssm params --all-regions --profile prod --cache 1h   # every region, cached
```

## See also

- [ssm-get.md](ssm-get.md) — fetch actual values, once you know the names
- [../environment.md](../environment.md) — `AJL_CACHE`/`AJL_PROFILES`/`AJL_REGIONS` for the fan-out + caching pattern above
