# Changelog

Notable changes to `ajl`, release by release. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versioning follows
[Semantic Versioning](https://semver.org/). See [DESIGN.md](DESIGN.md) for
the reasoning behind these changes, not just the summary.

## [0.4.0] - 2026-07-18

### Added
- `--describe`: for a List operation with a curated pairing, call the
  matching Describe/Get operation for every result and emit those records
  instead — no `--jq`/`--params-json` reshape needed. Curated across 62
  List→Describe/Get pairings in 21 services (athena, cloud9,
  cloudformation, cloudtrail, cloudwatch, dynamodb, ecs, eks, events, iam,
  kms, logs, opensearch, route53, s3, sagemaker, servicediscovery, ses,
  transfer).
- `--stamp-session` now also carries the resolved request params onto
  every record (e.g. the `cluster` an `ecs.ListTasks` response never
  echoes back), not just Profile/Region/Account.
- Age-identity guard messages (`ssm get` sealing, `--cache` on
  ssm/secretsmanager) now print the exact command to fix the problem
  instead of just naming it.
- `docs/environment.md` (every env var), `docs/data-files.md` (every file
  ajl reads or writes), `docs/request-flow.md` (the argv-to-JSONL decision
  path), `docs/commands/` (one reference page per custom command), and
  `docs/collision-survey-2026-07-18.md` (a full boto3-wide survey of
  `Type`/`Id`/`Name`/`Arn`/`Tags` field-name collisions, research for a
  still-open output-contract decision — see DESIGN.md).

### Fixed
- The result cache's key material was missing `--describe` — a cached
  plain list and a cached `--describe`'d run of the same command computed
  the same key and could replay each other's differently-shaped output.
- `--describe` resolved the boto3 client method via a Pascal→snake string
  transform, which mangles acronym-heavy operation names
  (`GetSAMLProvider` → `get_samlprovider` instead of `get_saml_provider`);
  now reads botocore's own authoritative method table instead of guessing.
- `--describe` had no per-batch error containment — one throttled or
  denied call could crash the entire run; now caught and reported per
  batch, matching every other error path in ajl.
- A `Get*`/`Describe*` response that never echoes back its own identifier
  (e.g. `iam.GetSAMLProvider`) no longer comes back with an empty `Id`/`Arn`.
- `--params-json` now remaps an incoming record's keys to a target
  operation's real member casing before coercion — fixes `ecs` and other
  services whose input members are lowerCamelCase, not PascalCase.
- `--stamp-session` now propagates automatically through every
  `--params-json` stage of a pipeline, not just the fan-out stage that
  originates it.
- Default `--rm-after` cache entry lifetime changed from 7d to 1h.

## [0.3.0] - 2026-07-17

### Added
- `--api-log` / `AJL_APILOG=1`: one JSONL record per underlying botocore
  call (service, operation, duration, HTTP status, retry attempts, item
  count, outcome) to `~/.local/state/ajl/apilog.jsonl`.
- `--version`: reports the version actually running — live `git describe`
  when run from a source checkout (so it can't go stale between
  reinstalls), the packaged version otherwise.
- `ajl ssm get` / `ssm update` / `ssm put` / `ssm params`: SSM Parameter
  Store wrappers — arg-picked API, chunked bulk fetches, field-level
  `SecureString` sealing (`AJLSEC:1:` envelopes), skip-if-unchanged
  updates, and an `ajl decrypt` standalone unseal filter.
- `--all` / `--all-profiles` / `--all-regions` / `--profiles` / `--regions`:
  multi-account/multi-region fan-out with account dedup and per-session
  error containment.
- `--cache TTL` / `AJL_CACHE`: whole-invocation result cache, gzip +
  optional age encryption; `ajl cache keygen|ls|clear`.
- `--learn` / `AJL_LEARN=1`: aws-cli-equivalent stderr line plus a JSONL
  audit record per invocation.
- `ajl s3 scan` / `ajl s3 list`: orchestrated recursive bucket inventory
  with adaptive range splitting, and the composable single-level listing
  primitive it's built from.
- `--jq` (post-shaping filter), `--stamp-session`, `--fetch-tags`.

## [0.2.0] - 2026-07-15

### Added
- Initial public shape: generic `<service> <operation>` dispatch over any
  boto3 API, with the `Type`/`Id`/`Name`/`Arn`/`Tags` normalizer driven by
  declarative `output.resources` model config (`output.jq` as the escape
  hatch), pagination (botocore paginators, marker-loop fallback),
  `--params-json` worker-pool fan-out, and the model generation/curation
  tooling (`tools/generate-model.py`, `tools/apply-resource-configs.py`).
