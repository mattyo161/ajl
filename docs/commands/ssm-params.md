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

The standard contract, run through the generic normalizer
(`normalize_resource` in `normalize.py`) rather than `ssm get`'s custom
narrower `_shape()` — `ajl.id` is the parameter name, `ajl.tags` is `{}`
(the API doesn't return tags on this call):

```json
{"Name": "/app/db/host",
 "ARN": "arn:aws:ssm:us-east-1:123456789012:parameter/app/db/host",
 "Type": "String", "Version": 3, "LastModifiedDate": "...", "Tier": "Standard",
 "ajl": {"type": "ssm:parameter", "id": "/app/db/host", "name": "/app/db/host",
         "arn": "arn:aws:ssm:us-east-1:123456789012:parameter/app/db/host", "tags": {}}}
```

SSM's own response has two fields that used to collide with ajl's
contract: `Type` (an exact match — the parameter's own `String`/
`StringList`/`SecureString`) and the all-caps `ARN` (a case-insensitive
near-miss ajl's old code silently dropped rather than renamed — a real
bug the collision survey found). Now that ajl's metadata lives in its own
trailing `ajl` object, neither collides with anything: both raw fields
pass through exactly as the API returned them, sitting right next to
`ajl.type`/`ajl.arn`. `ssm get`'s custom `_shape()` (see
[ssm-get.md](ssm-get.md)) benefits from the same fix — it no longer needs
the `ParameterType` alias it used to.

## Filtering and fan-out

Any real `describe-parameters` parameter works as a normal `--kebab-flag`:

```shell
ajl ssm params --parameter-filters '[{"Key":"Name","Option":"BeginsWith","Values":["/app/"]}]' --cache 1h
ajl ssm params --all-regions --profile prod --cache 1h   # every region, cached
```

## See also

- [ssm-get.md](ssm-get.md) — fetch actual values, once you know the names
- [../environment.md](../environment.md) — `AJL_CACHE`/`AJL_PROFILES`/`AJL_REGIONS` for the fan-out + caching pattern above
