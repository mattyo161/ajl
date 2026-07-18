# One call, start to finish: how ajl decides what to run and how to shape it

The decision points a single `ajl <service> <operation> ...` invocation walks
through, end to end — and where a `--params-json` pipeline's next stage
plugs back into the front of this same flow. Decisions worth remembering are
logged in [DESIGN.md](../DESIGN.md); this is the narrative, with a real
example (`ajl ecs list-clusters`) worked through every stage.

```mermaid
flowchart TD
    A["CLI argv\n--kebab-flag value"] --> B["parse_extra_options\nkebab -> PascalCase guess"]
    B --> C["coerce_params\nmodelconfig lookup + casing remap + type coercion"]
    C --> D["Runner.client\nboto3 session/client cache per (profile, region)"]
    D --> DD{"--describe and\noutput.describe set?"}
    DD -->|"yes"| DE["run_describe_chain\nlist, collect id_field, call Describe\nonce per id or per batch_size chunk"]
    DE --> E
    DD -->|"no"| E["pagination.iter_pages\npaginator, else marker-loop fallback"]
    E --> F{"shape_page decision"}
    F -->|"output.jq set"| G["jq program\n(escape hatch, always wins)"]
    F -->|"output.resources set"| H["normalize.iter_configured_resources\ndeclarative path walk"]
    F -->|"neither"| I["iter_default_resources\nheuristic: one top-level list"]
    G --> J["Emitter\n(+ StampEmitter, + JqEmitter)"]
    H --> J
    I --> J
    J --> K["JSONL on stdout"]
    K -.->|"--params-json - piped in"| B
```

## 1. Argv to params: two casing guesses, one from the model

`main.py`'s `parse_extra_options` turns `--cluster foo --desired-status RUNNING`
into `{"Cluster": "foo", "DesiredStatus": "RUNNING"}` — every CLI flag is
PascalCased, because that's what most boto3 operations expect. This is a
*guess*: it has no model in hand yet, just the flag name.

`coerce_params` is where the guess meets reality. It loads the operation's
real input shape (see below) and:

1. **Remaps every key to the model's actual member casing**, case-insensitive.
   Most AWS APIs are PascalCase, so `Cluster` already matches. A handful —
   ECS chief among them — define lowerCamelCase members (`cluster`, `tasks`,
   not `Cluster`/`Tasks`); without this remap boto3 rejects the call outright
   (`Unknown parameter in input: "Cluster"`). This bit us directly building
   the ecs pipeline below.
2. **Coerces each value** using the member's declared type (`"5"` -> `5` for
   an integer member, a JSON string -> a parsed list/dict for a
   list/structure/map member).
3. **With `filter_to_input=True`** (only in `--params-json` mode): drops any
   key that isn't a real input member at all. This is what lets a full
   `Type`/`Id`/`Name`/`Arn`/`Tags` ajl record get piped straight back into
   `--params-json -` — the properties the next operation doesn't want are
   silently dropped rather than erroring.

## 2. Finding the operation: per-service model, case-insensitive

`modelconfig.get_operation_config(service, operation_pascal)` loads
`ajl/models/<service>.json` (or `AJL_MODELS_DIR` override) and looks up the
operation. The first lookup is an exact match; if that misses, a
case-insensitive index catches the acronym-casing cases the kebab-to-Pascal
guess can't reconstruct (`list-open-id-connect-providers` guesses
`ListOpenIdConnectProviders`, but the real API name is
`ListOpenIDConnectProviders`). A miss here (no model for the service, or no
entry for the operation) just means no curated shaping — the call still
runs, output falls through to the heuristic path below.

## 3. Pagination: paginator first, marker loop as the fallback

`pagination.iter_pages` tries botocore's generated paginator first
(`client.can_paginate(operation)`) — it exists for virtually every list/describe
call and needs no model at all. When it doesn't exist, the model's
`input.markers`/`output.markers` metadata drives a manual marker loop
(`tools/generate-model.py` records these from the API's request/response
shapes when a paginator isn't available).

## 4. Shaping a page: three ways, one always wins

`shape_page` (`main.py`) checks the operation's `output` config in this order:

1. **`output.jq`** — a hand-written jq program. Always wins when present; the
   escape hatch for shapes the declarative config can't express.
2. **`output.resources`** — a declarative list of `{path, type, id, name,
   arn, arn_format, tags, scalar_as}` configs, applied by
   `normalize.iter_configured_resources`. This is the common case and is
   what `tools/apply-resource-configs.py` writes.
3. **Neither** — `iter_default_resources` heuristic: if the response has
   exactly one top-level list, stream its items unshaped (still gets
   `Type`/`Id`/`Name`/`Arn`/`Tags` — see below). Otherwise the raw response
   streams as-is.

### Worked example: `ecs.ListClusters`

The curated config is `r(["clusterArns"], "ecs:cluster", arn="clusterArn",
scalar_as="clusterArn")` — no `id`, no `name`, because `ListClusters`'
response is *only* an array of ARN strings (`clusterArns`), nothing else.
`scalar_as="clusterArn"` wraps each bare string into `{"clusterArn": "..."}`
so the path-walk and ARN lookup have a field to read.

That produces:

```json
{"Type":"ecs:cluster","Id":"salesagent-freewheel","Name":"","Arn":"arn:aws:ecs:us-east-1:381492092437:cluster/salesagent-freewheel","Tags":{},"clusterArn":"arn:aws:ecs:us-east-1:381492092437:cluster/salesagent-freewheel"}
```

Two things worth spelling out, since they come up every time a List*
operation only returns identifiers:

- **`Id` has no source field, so it falls back to the ARN's last path
  segment** (`normalize.py`'s documented fallback chain) — `Id` did not come
  from anywhere in the response *except* the ARN. Same rule everywhere: a
  resource with no natural id and a well-formed ARN always gets a sane `Id`.
- **`Name` is empty because `ListClusters` never returns a name** (and
  there's no `Tags` to fall back to — the list call doesn't return tags
  either). `DescribeClusters` (the paired describe call) does return
  `clusterName` and `tags`, which is why its curated config sets
  `name="clusterName", tags="tags"` and its output has a real `Name`.

## 5. Emitting and stamping

Shaped records go through `Emitter` (line-atomic stdout), optionally wrapped
by `TagMergeEmitter` (`--fetch-tags`), `JqEmitter` (`--jq`), and
`StampEmitter` (`--stamp-session`) — each wrapper adds one concern and passes
through to the next. `StampEmitter` adds `Profile`/`Region`/`Account` to
every record; separately, back in `run_operation` (before the record ever
reaches an emitter), `--stamp-session` also merges the resolved request
params onto it via `_stamp_params()` — a response often doesn't echo back
what it was asked for (`ecs.ListTasks` returns task ARNs, never the
`cluster` you asked about), so without this a downstream stage has to claw
the context back out of an ARN. `setdefault` means a genuine response field
always wins over a same-named request param. `should_stamp_session()` turns
all of this on automatically for fan-out (`--all`/...) *and* for any
`--params-json` stage (it's always mid-pipeline, so it re-stamps whatever
session info and request context its own input carried) —
`--no-stamp-session` opts out.

## 6. Chaining into `--params-json -`: what propagates, what doesn't

A record piped into `--params-json -` re-enters at step 1 as one line's
`line_params`. Three things now propagate automatically (fixed together,
see DESIGN.md): the model-casing remap (step 1), the session stamp, and the
request-param context `--stamp-session` merges on in step 5. That last one
compounds nicely across hops: once a `cluster` value is stamped onto a
`list-tasks` stage's task records, it's already sitting there under the
exact key name `ecs.DescribeTasks` expects, so it needs *no* `--jq` reshape
at all for a third hop — `filter_to_input` picks it straight up.

**What still has no generic fix**: there is no automatic mapping from a
shaped record's own identity fields (`Id`/`Arn`/`clusterArn`) to whatever
the *first* downstream operation calls its input — that first hop always
requires knowing the target operation's actual parameter name, which is
API-specific and has to be a `--jq` reshape:

```shell
ajl ecs list-clusters --all-regions --profile nri-customer --stamp-session \
  | jq -c '{cluster: .Id, Profile, Region, Account}' \
  | ajl ecs list-tasks --params-json - --stamp-session   # cluster now rides the rest of the way for free
```

It gets more API-specific still: `ecs.ListTasks`/`DescribeServices` take a
*singular* `cluster` (one call per cluster — fits the `--params-json`
one-line-per-call model directly), but `ecs.DescribeClusters`/`DescribeTasks`
also take a *plural* array (`clusters`/`tasks` — one call describes many, a
batch-describe shape, the same pattern `ssm get --names` chunks 10-at-a-time
in `ssm.py`). No amount of generic field-name guessing resolves that
difference; it's a property of each operation's shape, and it's exactly why
that hop still needs a `group_by(...) | nwise(10)`-style `--jq` reshape
(worked through live getting this ecs pipeline running) rather than piping
straight through. `--describe` (below) is the answer to exactly this,
driven by a curated `output.describe` config rather than teaching
`--params-json` more input formats.

## 7. `--describe`: skip the reshape entirely, when a pairing is curated

For a List operation with a curated `output.describe` config, `--describe`
does the List → jq-reshape → Describe dance internally: `run_operation`
checks for the config before step 4 ever runs, and if `--describe` is
passed, hands off to `run_describe_chain` instead of the normal shape+emit
path. It collects `id_field` off every shaped list record, then calls the
paired operation either once per id (`kind: scalar` — one call, no batch
form, this is the common case: `eks`, `iam`, `sagemaker`, `transfer` are
entirely this shape) or in `batch_size`-sized chunks (`kind: array` — `ecs`,
`ecr`, `athena`; the same chunking `ssm.py` uses for `get --names`), fanning
across the same worker pool `--params-json` uses either way. A `scope` list
in the config (e.g. `RoleName` for `iam.GetRolePolicy`, `cluster` for
`ecs.DescribeTasks`) forwards List-call params the Describe call also needs
but the list response never repeats — taken from the *original* call's own
params, not stamped from a record, since by then the record already *is*
the describe result:

```shell
ajl iam list-role-policies --role-name my-role --describe   # one GetRolePolicy per policy name
ajl ecs list-clusters --all-regions --profile nri-customer --describe | sd ecs-clusters
```

No `--jq` reshape, no `--params-json` pipe stage, no casing to get right —
`--describe` is the same shape of fix as `--stamp-session`'s request-param
carrying (§5): attach what a bare response leaves out, generically, instead
of by hand each time.