# Changelog

Notable changes to `ajl`, release by release. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versioning follows
[Semantic Versioning](https://semver.org/). See [DESIGN.md](DESIGN.md) for
the reasoning behind these changes, not just the summary.

## [0.8.0] - 2026-07-20

### Added
- Persistent cross-process account-id cache (`src/ajl/accountcache.py`):
  `Runner.account()` now checks `~/.local/state/ajl/accounts.json`
  (keyed by named profile, 24h TTL) before calling
  `sts.GetCallerIdentity` — collapses the redundant resolution
  `inventory.sh`'s ~150-process-per-run fan-out was paying on every run.
- `tools/build-duckdb.py`: standalone script (PEP 723 header, no new
  runtime dependency of ajl itself) that loads a `tools/inventory.sh`
  run's output, plus `--api-log` telemetry, into a DuckDB file for
  ad-hoc analysis.
- `tools/security-checks.sql`: 11 generic, reusable monitoring queries
  (public EKS endpoints, expired certs, over-privileged instance
  profiles, open security groups, CloudTrail posture, S3 versioning,
  RDS Multi-AZ, IAM access-key rotation, Secrets Manager rotation)
  against the schema `build-duckdb.py` produces.
- `docs/duckdb-analysis.md`: workflow and DuckDB-specific gotchas for
  the two tools above.

### Changed
- `Runner.client()`'s retry mode is no longer a single global default.
  `mode: adaptive` is now scoped to services/operations with real,
  observed throttling (`ssm`, `s3`, and `efs.DescribeMountTargets`
  specifically) instead of applying to every service; everything else
  uses `mode: standard` with a low `max_attempts`, so a call that was
  never actually being throttled fails fast instead of paying adaptive's
  retry cost speculatively.
- `tools/inventory.sh`: dropped `sagemaker list-human-task-uis` from the
  bulk run (confirmed zero throttling/permissions errors, just an unused
  feature); added `--cache 24h` to `transfer list-security-policies`
  (fixed, account-independent AWS catalog) and
  `efs describe-mount-targets` (mount targets change rarely).

## [0.7.0] - 2026-07-19

### Changed
- **BREAKING**: replaced the top-level `Type`/`Id`/`Name`/`Arn`/`Tags`/`Uri`
  output shape (and, under `--stamp-session`, top-level `Profile`/`Region`/
  `Account` + forwarded request params) with a single trailing, lowercase
  `ajl` object: `ajl.{type,id,name,arn,tags,uri}` +
  `ajl.stamp.{profile,region,account,...}`, appended last on every record.
  Every raw AWS field now passes through completely untouched, in its
  original casing, with no renaming or dropping — closes the collision
  class documented in `docs/collision-survey-2026-07-18.md` (~50% of
  boto3 services exact-collided, ~42% near-missed case-insensitively,
  against the old top-level names) and the one existing data-loss bug
  (a colliding raw `ARN` field was silently dropped). No transition
  period; every consumer must read the new shape. See DESIGN.md.

### Added
- Curated `iam.ListAccessKeys` / `iam.GetAccessKeyLastUsed` (access-key
  age/rotation), wired into `tools/inventory.sh` as two joinable files
  (not `--describe`, which would have overwritten `CreateDate`/`Status`
  with the last-used response).

### Fixed
- `tools/inventory.sh`: `ssm params` and `secretsmanager list-secrets`
  now pass `--no-cache` — both were silently returning empty results
  under the script's global `AJL_CACHE`, since ssm/secretsmanager calls
  refuse to run under `--cache` without an age key configured.

## [0.6.0] - 2026-07-18

### Added
- Curated `Type`/`Id`/`Name`/`Arn`/`Tags` output shaping for cloudfront
  (distributions, streaming distributions, invalidations, functions,
  origin access controls, public keys, key groups, field-level-encryption
  configs/profiles, realtime log configs, continuous deployment policies,
  cache/origin-request/response-headers policies) and elasticache (cache
  clusters, replication groups, subnet/parameter groups, snapshots,
  serverless caches, users, user groups) — both new.
- `--describe` pairing for `opensearch.ListDomainNames` → `DescribeDomains`
  (was missing from the original curation pass).
- `tools/inventory.sh`: kms, plus a batch of already-curated services that
  had never been wired in — cloudformation, cloudtrail, cloudwatch,
  events, logs, sagemaker, servicediscovery, transfer, secretsmanager,
  opensearch. 37 services total now covered.

### Fixed
- Cross-checked coverage against an older aws-cli+jq inventory tool and
  closed the gap it surfaced: elasticache had no curation at all, and a
  real ElastiCache Redis cluster on the reference account was invisible
  to the inventory until this release.

## [0.5.0] - 2026-07-18

### Added
- Curated `Type`/`Id`/`Name`/`Arn`/`Tags` output shaping (and, where
  paired, `--describe`) for 10 more services: sqs, sns, ses, wafv2, acm,
  redshift, workspaces, docdb, dynamodb, athena.
- s3: curated all 17 per-bucket `Get*` config calls (versioning,
  encryption, policy, policy status, lifecycle, CORS, notification,
  replication, logging, accelerate, ownership controls, tagging,
  location, request payment, ACL, website, public-access-block, object
  lock) — none of these echo `Bucket` back in their response, so `Id`/
  `Name`/`Arn` come back deliberately blank; `--stamp-session`'s `Bucket`
  (from the request params) is the join key back to the bucket list.
- `tools/inventory.sh`: a live, ajl-native multi-service AWS inventory
  script (25 services total across this and the prior release), run
  against a real account as an established regression baseline ahead of
  the `_ajl_`-namespace output-contract decision (see DESIGN.md).

### Fixed
- `sns.GetTopicAttributes` curation used `TopicArn` as the id field,
  putting the full ARN into `Id` instead of the short topic name — now
  uses `arn=` so `Id` falls back to the ARN's last segment correctly.

## [0.4.1] - 2026-07-18

### Added
- This file — backfilled through v0.2.0 rather than starting bare.

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
