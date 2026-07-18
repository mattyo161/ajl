# Type/Id/Name/Arn/Tags collision survey (2026-07-18)

Research only — not a decision, not implemented. Commissioned to inform the
open "stop renaming AWS's own fields on a collision, namespace ajl's instead
(`_ajl_`)" decision (see [DESIGN.md](../DESIGN.md) and the backlog). Full
verbatim findings from the background research agent, preserved here since
the backlog only carries a condensed summary.

## Method

Scanned all 416 service directories under the ajl venv's installed
`botocore/data/` (latest API version per service), 17,271 operations total.
For each operation's output shape, checked both (a) the response shape's
own top-level members (singular Get/Describe pattern) and (b) the top-level
members of any list-wrapped item shape (plural Describe/List pattern) —
i.e. exactly the level ajl's `normalize_resource()` operates on. Classified
each member name as an **exact** match against `Type`/`Id`/`Name`/`Arn`/
`Tags`, or a **case-insensitive-only** match (different case, not identical).

## Top-line verdict: common, not rare

- **210 distinct services** (of 416, ~50%) have at least one exact-case collision.
- **173 distinct services** (~42%) have at least one case-insensitive-only near-miss.
- **355 distinct services** (~85%) have at least one of the two.
- 3,261 exact-collision hits and 3,238 case-insensitive-only hits, across operations.
- The `ID`/`ARN`-vs-`Id`/`Arn` scenario is **not theoretical** — it's real and recurring (see below).

## Exact-case collisions (already handled today by rename-to-`Original<Key>`)

| ajl key | hits | distinct services | note |
|---|---|---|---|
| Tags | 721 | 170 | mostly benign — ajl auto-detects a raw `Tags` list/dict as the tag source, so it's consumed rather than truly colliding |
| Name | 980 | 141 | AWS's own idiomatic `Name` field, same convention ajl borrowed |
| Arn | 716 | 73 | same — AWS's own `Arn`/`ARN` convention |
| Id | 579 | 74 | same |
| Type | 265 | **87** | the interesting one — AWS's `Type` usually means something *else* (resource subtype, data type), so this is a real semantic collision, not just naming overlap |

Worst single-service offenders (exact-collision op counts): **MediaLive
(264)**, EC2 (142), Connect (131), Greengrass (128), QuickSight (121) —
these are services with many resource kinds, each naturally exposing
`Arn`/`Id`/`Name`/`Tags` at the top level, which is *expected*: ajl's five
names were chosen to match AWS's own dominant convention, so high
exact-collision counts mostly validate that choice rather than expose a
flaw.

## Case-insensitive-only near-misses (invisible to today's `key in result` check)

| ajl key | hits | distinct services |
|---|---|---|
| Name | 971 | 114 |
| Id | 743 | 71 |
| Arn | 736 | 95 |
| Tags | 543 | 135 |
| Type | 245 | 66 |

Breaking down by actual spelling: 971 `name`, 739 `id`, 673 `arn`, 543
`tags`, 245 `type` (all-lowercase) vs. only **63 `ARN`** and **4 `ID`**
(all-caps). The lowercase cluster is really one phenomenon: services whose
wire protocol uses camelCase field names throughout (DataZone, VPC Lattice,
Omics, Lightsail, API Gateway, RoboMaker, mgn, IoTFleetWise...) — not
isolated typos, but a systemic per-service naming style. The all-caps
`ARN`/`ID` cluster is the genuinely surprising near-miss pattern:
**ElastiCache, MemoryDB, SSM, S3 (CORS/lifecycle rule `ID`), CloudSearch,
OpenSearch/Elasticsearch, WorkMail, Secrets Manager, WAFV2, Service
Catalog** all emit an all-caps `ARN` or `ID` field sitting right next to
where ajl's `Arn`/`Id` would land — this is the scenario Matt asked about,
confirmed real.

## Notable finding: SSM is a double case

SSM's `Parameter`/`ParameterMetadata` shapes (used by `GetParameters`,
`DescribeParameters`) have **both** issues in one record: an exact `Type`
collision (the one Matt already knew about) *and* a case-insensitive `ARN`
field — so it's a two-for-one worst case.

## A pre-existing gap already in ajl's code

`normalize.py`'s `if key.lower() == "arn" and key != "Arn": continue` shows
Matt already anticipated the `ARN` near-miss for exactly one key — but the
fix silently **drops** the raw field entirely rather than renaming it to
`OriginalARN`. Unlike the exact-collision path, there's no fallback
preservation: if the raw `ARN` value ever differs from ajl's computed
`Arn` (plausible for MemoryDB/ElastiCache/SSM), that data is lost today,
silently, with no diagnostic. `Id`/`Name`/`Type`/`Tags` have no equivalent
case-insensitive handling at all — those near-misses land as separate,
un-normalized sibling keys.

## Bearing on the (a) vs (b) decision

Exact collisions are common but largely *already* AWS's own convention
colliding with ajl's chosen names — the current rename scheme handles them
fine and is arguably "correct" behavior, not a design smell. The
case-insensitive gap is the real argument for option (b) (the `_ajl_`
namespace): it's not a handful of edge cases (66–135 services per key), it
spans both a systemic style difference (whole-service camelCase) and a
genuine near-miss pattern (`ARN`/`ID` in otherwise-PascalCase shapes), and
the one place it's been patched (`Arn`) was patched by silently discarding
data rather than preserving it.

## Caveats

- Method scanned static botocore model shapes, not live API responses —
  actual field presence at runtime can differ (optional fields, API
  version drift).
- "Distinct services" counts a service once regardless of how many
  operations within it collide — a service with one colliding operation
  and a service with fifty both count as 1.
- Did not check nested (non-top-level) field collisions — out of scope,
  since `normalize_resource()` never looks past the top level of one
  shaped resource item.
