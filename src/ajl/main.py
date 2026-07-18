"""ajl CLI entry point.

Usage:
    ajl <service> <operation> [--param value ...] [options]
    ... | ajl --params-json - [defaults...] [options]

Operations and parameters are given aws-cli style in kebab-case and converted
to the PascalCase boto3 expects; parameter values are coerced using the
service model's input member types (integers, booleans, lists, JSON).
"""

import argparse
import contextlib
import os
import pathlib
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
import caseconverter
import jq as jqlib
import jsonlines
import orjson
from botocore.config import Config as BotoConfig

from . import __version__, apilog, learn, seal
from .cache import ResultCache, run_cache_command
from .debug import cache_hit
from .modelconfig import get_operation_config
from .normalize import id_from_arn, iter_configured_resources, iter_default_resources
from .pagination import iter_pages
from .tags import TagMergeEmitter

DEFAULT_PROFILE = os.environ.get("AWS_PROFILE", os.environ.get("AWS_DEFAULT_PROFILE"))
DEFAULT_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))


def _find_repo_root():
    path = pathlib.Path(__file__).resolve().parent
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def runtime_version():
    """The version actually running, not just the packaged one.

    setuptools-scm bakes __version__ into _version.py at build/install time;
    in this repo's edit-without-reinstalling dev loop that file goes stale
    the moment a new commit lands, so it can silently under-report what's
    checked out. When the running code lives in a git checkout (an editable
    install pointed at source), ask git directly instead — always current,
    includes the commit and a -dirty marker for uncommitted changes. Falls
    back to the packaged version for a real installed distribution, where
    there's no adjacent .git and the baked-in version is accurate.
    """
    repo_root = _find_repo_root()
    if repo_root is not None:
        try:
            described = subprocess.run(
                ["git", "-C", str(repo_root), "describe", "--tags", "--always", "--dirty"],
                capture_output=True, text=True, timeout=2,
            )
            if described.returncode == 0 and described.stdout.strip():
                return described.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return __version__


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ajl",
        description="Stream AWS API responses as JSON Lines with consistent "
        "Type/Id/Name/Arn/Tags properties.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="store_true", default=False,
                        help="print the running version and exit — git-derived "
                        "(tag/commit count/short sha, +dirty if uncommitted) "
                        "when run from a source checkout, so it stays accurate "
                        "between installs; the packaged version otherwise")
    parser.add_argument("--profile", type=str, default=None)
    parser.add_argument("--region", type=str, default=None)
    parser.add_argument("--params-json", type=str, metavar="FILE|-",
                        help="read JSONL request params ('-' for stdin); each line may "
                        "override 'client', 'operation', 'profile' and 'region'")
    parser.add_argument("--no-parse", action="store_true", default=False,
                        help="emit raw responses without shaping")
    parser.add_argument("--no-paginate", action="store_true", default=False,
                        help="only make a single API call")
    parser.add_argument("--max-items", type=int, default=None,
                        help="stop after emitting this many resources per request")
    parser.add_argument("--fetch-tags", action="store_true", default=False,
                        help="batch-fetch missing Tags via the Resource Groups Tagging API")
    parser.add_argument("--describe", action="store_true", default=False,
                        help="for a List operation with a curated pairing, call the "
                        "matching Describe/Get operation for every result and emit "
                        "those records instead — no --params-json reshape needed")
    parser.add_argument("--jq", type=str, default=None, metavar="PROGRAM",
                        help="post-shaping jq filter applied to every record; empty "
                        "output drops the record, multiple outputs emit multiple lines")
    parser.add_argument("--stamp-session", action="store_true", default=False,
                        help="add Profile/Region/Account plus the resolved request "
                        "params (e.g. the 'cluster' a response doesn't echo back) to "
                        "every record, so piped --params-json stages reuse the same "
                        "credentials and keep the context a bare response drops — on "
                        "by default for fan-out (--all/...) and for --params-json "
                        "itself, so it propagates through a multi-stage pipe")
    parser.add_argument("--no-stamp-session", action="store_true", default=False,
                        help="disable the Profile/Region/Account + request-param "
                        "stamp for this run, overriding the fan-out/--params-json "
                        "default")
    parser.add_argument("--workers", type=int, default=8,
                        help="parallel requests in --params-json mode (default 8; "
                        "use 1 to preserve input order)")
    parser.add_argument("--all-profiles", action="store_true", default=False,
                        help="run across every profile (AJL_PROFILES or ~/.aws/config), "
                        "de-duped by account; stamps records with their session")
    parser.add_argument("--all-regions", action="store_true", default=False,
                        help="run across every enabled region per account")
    parser.add_argument("--all", action="store_true", default=False,
                        help="shorthand for --all-profiles --all-regions")
    parser.add_argument("--regions", nargs="*", default=None, metavar="REGION",
                        help="run across exactly these regions (space or comma "
                        "separated); opt-in regions allowed when named here")
    parser.add_argument("--profiles", nargs="*", default=None, metavar="PROFILE",
                        help="run across exactly these profiles (space or comma "
                        "separated)")
    parser.add_argument("--cache", type=str, default=None, metavar="TTL",
                        help="serve cached results younger than TTL (e.g. 15m, 2h) "
                        "and store fresh ones, gzipped (+age encrypted when "
                        "AJL_AGE_* is set) under ~/.cache/ajl; AJL_CACHE sets a "
                        "default; manage with 'ajl cache ls|clear|keygen'")
    parser.add_argument("--no-cache", action="store_true", default=False,
                        help="disable the result cache for this run, overriding "
                        "an AJL_CACHE default")
    parser.add_argument("--refresh", "--recache", "--re-cache", action="store_true",
                        default=False, dest="refresh",
                        help="with --cache: skip reading the cache, still store "
                        "the fresh result")
    parser.add_argument("--rm-after", type=str, default=None, metavar="DUR",
                        help="cache entry lifetime before automatic cleanup "
                        "(default AJL_CACHE_RM_AFTER or 7d)")
    parser.add_argument("--learn", action="store_true", default=False,
                        help="print the aws-cli equivalent to stderr and append "
                        "an audit record (duration, cache status, scan slices) "
                        "to the learn log (AJL_LEARN=1 enables globally)")
    parser.add_argument("--no-learn", action="store_true", default=False,
                        help="disable the learn log for this run, overriding an "
                        "AJL_LEARN default")
    parser.add_argument("--api-log", action="store_true", default=False,
                        help="append one JSONL record per underlying AWS API call "
                        "(service, operation, duration, HTTP status, retry "
                        "attempts, outcome) to AJL_APILOG_FILE (default "
                        "~/.local/state/ajl/apilog.jsonl); AJL_APILOG=1 enables "
                        "globally")
    parser.add_argument("--no-api-log", action="store_true", default=False,
                        help="disable the api-log for this run, overriding an "
                        "AJL_APILOG default")
    parser.add_argument("--verbose", action="store_true", default=False)
    return parser


def parse_extra_options(tokens, verbose=False):
    """Convert remaining ``--kebab-case value...`` tokens to a params dict."""
    options = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("--"):
            key = caseconverter.pascalcase(token[2:])
            values = []
            i += 1
            while i < len(tokens) and not tokens[i].startswith("--"):
                values.append(tokens[i])
                i += 1
            if not values:
                options[key] = True
            elif len(values) == 1:
                options[key] = values[0]
            else:
                options[key] = values
        else:
            print(f"ajl: ignoring unexpected argument {token!r}", file=sys.stderr)
            i += 1
    return options


def _parse_json_value(value):
    try:
        return orjson.loads(value)
    except orjson.JSONDecodeError:
        return value


def coerce_param(value, member):
    """Coerce a CLI string using the model's input member type."""
    if not isinstance(value, (str, list)):
        return value
    member_type = (member or {}).get("type")
    if isinstance(value, list):
        return [_parse_json_value(item) if isinstance(item, str) else item for item in value]
    if member_type in ("integer", "long"):
        return int(value)
    if member_type in ("float", "double"):
        return float(value)
    if member_type == "boolean":
        return value.lower() in ("1", "true", "yes")
    if member_type in ("list", "structure", "map"):
        parsed = _parse_json_value(value)
        if member_type == "list" and not isinstance(parsed, list):
            return [parsed]
        return parsed
    if member_type is None and value[:1] in ("{", "["):
        return _parse_json_value(value)
    return value


def coerce_params(params, service, operation_pascal, filter_to_input=False):
    """Coerce param values; optionally drop params the operation doesn't
    accept (used in --params-json mode so records emitted by a previous ajl
    stage — with Type/Id/Name/Arn/Tags etc. — can be piped back in as-is).

    Keys are remapped to the model's actual member casing first: most boto3
    operations use PascalCase (matching the guess `--kebab-flag` -> PascalCase
    makes), but a few APIs — ecs among them — define lowerCamelCase members
    (`cluster`, `tasks`), which boto3 rejects verbatim if handed `Cluster`."""
    operation_cfg = get_operation_config(service, operation_pascal) or {}
    members = (operation_cfg.get("input") or {}).get("members") or {}
    if members:
        members_by_lower = {name.lower(): name for name in members}
        params = {members_by_lower.get(key.lower(), key): value for key, value in params.items()}
    if filter_to_input and members:
        params = {key: value for key, value in params.items() if key in members}
    return {
        key: coerce_param(value, members.get(key))
        for key, value in params.items()
        if value is not None
    }


def json_default(obj):
    return str(obj)


def dumps(obj):
    return orjson.dumps(obj, default=json_default).decode()


def strip_metadata(response):
    return {key: value for key, value in response.items() if key != "ResponseMetadata"}


class Emitter:
    """Line-atomic JSONL writer shared across worker threads.

    Flushes are batched on a short interval rather than per line — at
    inventory volumes a flush syscall per record dominates CPU while a 100ms
    delay is imperceptible in a pipe. flush() forces the tail out.
    """

    def __init__(self, stream=None, flush_interval=0.1):
        self.stream = stream or sys.stdout
        self.lock = threading.Lock()
        self.flush_interval = flush_interval
        self._last_flush = 0.0

    def emit(self, obj, session_key=None):
        line = obj if isinstance(obj, str) else dumps(obj)
        with self.lock, contextlib.suppress(BrokenPipeError, ValueError):
            # ValueError = write after the stream closed during teardown; a
            # late worker thread emitting then is harmless, not an error
            self.stream.write(line + "\n")
            now = time.monotonic()
            if now - self._last_flush >= self.flush_interval:
                self.stream.flush()
                self._last_flush = now

    def flush(self):
        with self.lock, contextlib.suppress(BrokenPipeError):
            self.stream.flush()


class Runner:
    """Caches boto3 sessions/clients per (profile, region) for reuse and
    thread safety."""

    def __init__(self, default_profile=None, default_region=None, verbose=False,
                 api_log=False):
        self.default_profile = default_profile
        self.default_region = default_region
        self.verbose = verbose
        self.api_log = api_log
        self._sessions = {}
        self._clients = {}
        self._accounts = {}
        self._jq_cache = {}
        self._lock = threading.RLock()

    def session_key(self, profile=None, region=None):
        return (profile or self.default_profile, region or self.default_region)

    def session(self, session_key):
        with self._lock:
            session = self._sessions.get(session_key)
            if session is None:
                profile, region = session_key
                session = boto3.Session(profile_name=profile, region_name=region)
                self._sessions[session_key] = session
            else:
                cache_hit("session", session_key)
            return session

    def client(self, session_key, service):
        with self._lock:
            key = (session_key, service)
            if key not in self._clients:
                # adaptive retries so throttle-heavy APIs (ssm describe-parameters
                # especially) self-tune; bounded timeouts so an unreachable
                # endpoint (e.g. a disabled opt-in region) fails in seconds
                # instead of hanging on botocore's 60s default connect timeout
                config = BotoConfig(
                    retries={"mode": "adaptive", "max_attempts": 10},
                    connect_timeout=float(os.environ.get("AJL_CONNECT_TIMEOUT", "5")),
                    read_timeout=float(os.environ.get("AJL_READ_TIMEOUT", "30")),
                )
                client = self.session(session_key).client(service, config=config)
                if self.api_log:
                    profile, region = session_key
                    apilog.attach(client, service, profile, region)
                self._clients[key] = client
            else:
                cache_hit("client", key)
            return self._clients[key]

    def account(self, session_key):
        with self._lock:
            if session_key in self._accounts:
                cache_hit("account", session_key)
                return self._accounts[session_key]
        try:
            sts = self.client(session_key, "sts")
            account = sts.get_caller_identity()["Account"]
        except Exception as exc:
            print(f"ajl: unable to resolve account id: {exc}", file=sys.stderr)
            account = ""
        with self._lock:
            self._accounts[session_key] = account
        return account

    def compiled_jq(self, program, jq_args):
        key = (program, tuple(sorted(jq_args.items())))
        with self._lock:
            if key not in self._jq_cache:
                self._jq_cache[key] = jqlib.compile(program, args=jq_args)
            else:
                cache_hit("jq", program[:60])
            return self._jq_cache[key]


class JqEmitter:
    """Emitter wrapper applying a global --jq filter to every record.

    Empty jq output drops the record; multiple outputs emit multiple lines;
    string outputs print raw (like jq -r), handy for extracting single fields.
    """

    def __init__(self, emitter, program_text):
        self.emitter = emitter
        self.program = jqlib.compile(program_text)

    def emit(self, record, session_key=None):
        for out in self.program.input_text(dumps(record)):
            self.emitter.emit(out, session_key)

    def flush(self):
        self.emitter.flush()


class StampEmitter:
    """Emitter wrapper adding Profile/Region/Account to every record so a
    downstream --params-json stage routes back to the same session."""

    def __init__(self, emitter, runner):
        self.emitter = emitter
        self.runner = runner

    def emit(self, record, session_key=None):
        if isinstance(record, dict):
            profile, region = session_key or self.runner.session_key()
            record["Profile"] = profile or ""
            record["Region"] = region or ""
            record["Account"] = self.runner.account(session_key or self.runner.session_key())
        self.emitter.emit(record, session_key)

    def flush(self):
        self.emitter.flush()


def should_stamp_session(options, fanning):
    """Whether records get the Profile/Region/Account stamp: on for fan-out
    (--all/...), which originates it, and for --params-json, which is always
    mid-pipeline — otherwise the stamp a fan-out stage attached silently
    drops after one hop and a multi-stage --all | ... | ... chain can never
    reach its last stage. --no-stamp-session forces it off regardless."""
    if options.no_stamp_session:
        return False
    return bool(fanning or options.params_json or options.stamp_session)


def pop_session_fields(line_params):
    """Pop the session routing fields from a --params-json line (both the
    lowercase form and the PascalCase form written by --stamp-session)."""
    profile = line_params.pop("profile", None) or line_params.pop("Profile", None)
    region = line_params.pop("region", None) or line_params.pop("Region", None)
    return profile or None, region or None


def shape_page(page, operation_cfg, context, runner, session_key):
    """Yield the JSONL records for one response page."""
    output_cfg = (operation_cfg or {}).get("output") or {}
    custom_jq = output_cfg.get("jq")
    if custom_jq:
        if "$account" in custom_jq and not context.get("account"):
            context["account"] = runner.account(session_key)
        program = runner.compiled_jq(
            custom_jq,
            {
                "account": context.get("account") or "",
                "region": context.get("region") or "",
                "partition": context.get("partition") or "",
            },
        )
        yield from program.input_text(dumps(page))
        return

    resource_cfgs = output_cfg.get("resources")
    if resource_cfgs:
        needs_account = any(
            "{account}" in (cfg.get("arn_format") or "") for cfg in resource_cfgs
        )
        if needs_account and not context.get("account"):
            context["account"] = runner.account(session_key)
        yield from iter_configured_resources(page, resource_cfgs, context)
        return

    yield from iter_default_resources(page)


def _stamp_params(record, params):
    """Merge the resolved request params onto a record (--stamp-session):
    a response often doesn't echo back what it was asked for (ecs ListTasks
    returns task ARNs, never the cluster you asked about), so a downstream
    --params-json stage — or just storage — would otherwise lose that
    context. setdefault so a real response field never gets clobbered."""
    if isinstance(record, dict):
        for key, value in params.items():
            record.setdefault(key, value)
    return record


def _iter_operation_records(client, operation_snake, operation_cfg, params, context,
                             runner, session_key, options):
    """Yield one call's records: raw pages under --no-parse, else shaped resources."""
    for page in iter_pages(
        client,
        operation_snake,
        params,
        operation_cfg,
        paginate=not options.no_paginate,
        verbose=runner.verbose,
    ):
        page = strip_metadata(page)
        if options.no_parse:
            yield page
            continue
        yield from shape_page(page, operation_cfg, context, runner, session_key)


def run_operation(runner, emitter, options, service, operation, params, session_key):
    """Execute one (possibly paginated) API call and emit its resources."""
    operation_snake = caseconverter.snakecase(operation)
    operation_pascal = caseconverter.pascalcase(operation)
    client = runner.client(session_key, service)
    operation_cfg = get_operation_config(service, operation_pascal)
    context = {
        "partition": client.meta.partition,
        "region": client.meta.region_name,
        "account": "",
    }
    if runner.verbose:
        print(
            f"ajl: {service}.{operation_snake} params={params} session={session_key}",
            file=sys.stderr,
        )

    if getattr(options, "describe", False):
        describe_cfg = (operation_cfg or {}).get("output", {}).get("describe")
        if describe_cfg:
            run_describe_chain(runner, emitter, options, service, operation_cfg,
                                describe_cfg, context, client, operation_snake,
                                params, session_key)
            return
        print(f"ajl: --describe: no curated List->Describe pairing for "
              f"{service}.{operation_pascal} — running the plain list instead. "
              f"Some lists (iam.ListRoles among them) already return full "
              f"details and need no pairing; if this one doesn't, see "
              f"AGENTS.md's \"Pair a List op with its Describe/Get\"",
              file=sys.stderr)

    emitted = 0
    for record in _iter_operation_records(client, operation_snake, operation_cfg, params,
                                           context, runner, session_key, options):
        if options.stamp_session:
            _stamp_params(record, params)
        emitter.emit(record, session_key)
        emitted += 1
        if options.max_items and emitted >= options.max_items:
            return


def run_describe_chain(runner, emitter, options, service, list_operation_cfg, describe_cfg,
                        context, client, list_operation_snake, list_params, session_key):
    """``--describe``: list, then call the paired Describe/Get operation for
    every result instead of emitting the (usually bare-id) list records.

    ``kind: scalar`` calls the describe operation once per id (the common
    case — most AWS List/Describe pairs have no batch form at all, so this
    is exactly what a hand-written --params-json chain already does, just
    generated instead of piped). ``kind: array`` chunks ids into
    ``batch_size``-sized groups and calls once per chunk, the same pattern
    ``ssm.py`` uses for ``get --names``. Either way, every call fans out
    across the same worker pool --params-json uses."""
    ids = [
        record.get(describe_cfg["id_field"])
        for record in _iter_operation_records(client, list_operation_snake, list_operation_cfg,
                                               list_params, context, runner, session_key, options)
        if isinstance(record, dict) and record.get(describe_cfg["id_field"])
    ]
    if not ids:
        return

    # describe_cfg["operation"] is already the real PascalCase operation name
    # (that's what gets curated) — no kebab/Pascal round-trip needed for the
    # model lookup. The boto3 *method* name is a different story: deriving it
    # via caseconverter.snakecase(PascalName) mangles consecutive-capital
    # acronyms (GetSAMLProvider -> get_samlprovider, not get_saml_provider);
    # client.meta.method_to_api_mapping is botocore's own authoritative
    # method-name table, so reverse it instead of guessing.
    describe_operation = describe_cfg["operation"]
    describe_operation_cfg = get_operation_config(service, describe_operation)
    api_to_method = {op: name for name, op in client.meta.method_to_api_mapping.items()}
    describe_operation_snake = api_to_method.get(describe_operation)
    if describe_operation_snake is None:
        print(f"ajl: --describe: {service} has no operation {describe_operation!r} "
              f"— check the curated describe config", file=sys.stderr)
        return
    scope_params = {key: list_params[key] for key in describe_cfg.get("scope", [])
                     if key in list_params}

    if describe_cfg["kind"] == "array":
        batch_size = describe_cfg.get("batch_size", 100)
        id_batches = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]
    else:
        id_batches = [[one_id] for one_id in ids]  # one call per id
    param_batches = [
        coerce_params({**scope_params, describe_cfg["param"]:
                       batch if describe_cfg["kind"] == "array" else batch[0]},
                      service, describe_operation, filter_to_input=True)
        for batch in id_batches
    ]
    print(f"ajl: --describe: {len(ids)} id(s) -> {len(param_batches)} "
          f"{describe_operation} call(s)", file=sys.stderr)

    def run_one(id_batch, describe_params):
        try:
            for record in _iter_operation_records(client, describe_operation_snake,
                                                   describe_operation_cfg, describe_params,
                                                   context, runner, session_key, options):
                if options.stamp_session:
                    # only the scope (e.g. cluster), never the identifier/batch
                    # itself — a record's own fields already say who it is, and
                    # for kind="array" the identifier is the whole batch, which
                    # would otherwise get redundantly attached to every record in it
                    _stamp_params(record, scope_params)
                if (describe_cfg["kind"] == "scalar" and isinstance(record, dict)
                        and not record.get(describe_cfg["id_field"])):
                    # many Get* calls don't echo back the identifier you gave
                    # them (you already know it) — without this a described
                    # record can come back with an empty Id/Arn entirely
                    record[describe_cfg["id_field"]] = id_batch[0]
                    if describe_cfg["id_field"] == "Arn" and not record.get("Id"):
                        record["Id"] = id_from_arn(id_batch[0])
                emitter.emit(record, session_key)
        except Exception as exc:
            shown = id_batch if len(id_batch) <= 3 else [*id_batch[:3], "..."]
            print(f"ajl: --describe: {describe_operation} failed for {shown}: {exc}",
                  file=sys.stderr)

    workers = max(1, getattr(options, "workers", 1))
    pairs = list(zip(id_batches, param_batches))
    if workers > 1 and len(pairs) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda pair: run_one(*pair), pairs))
    else:
        for id_batch, one_batch in pairs:
            run_one(id_batch, one_batch)


def run_params_json(runner, emitter, options, service, operation, base_params):
    """Stream request params from JSONL and run them on a worker pool."""
    if options.params_json == "-":
        source = sys.stdin
    else:
        source = open(options.params_json)

    errors = 0
    error_lock = threading.Lock()

    def run_line(line_params):
        nonlocal errors
        line_service = line_params.pop("client", None) or line_params.pop("service", None) or service
        line_operation = line_params.pop("operation", None) or operation
        profile, region = pop_session_fields(line_params)
        session_key = runner.session_key(profile, region)
        try:
            if not line_service or not line_operation:
                raise ValueError("line is missing 'client'/'operation' and no defaults were given")
            params = coerce_params(
                {**base_params, **line_params},
                line_service,
                caseconverter.pascalcase(line_operation),
                filter_to_input=True,
            )
            run_operation(runner, emitter, options, line_service, line_operation, params, session_key)
        except Exception as exc:
            with error_lock:
                errors += 1
            print(f"ajl: request failed ({line_service}.{line_operation}): {exc}", file=sys.stderr)

    workers = max(1, options.workers)
    reader = jsonlines.Reader(source)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for line_params in reader:
                if not isinstance(line_params, dict):
                    print(f"ajl: skipping non-object line: {line_params!r}", file=sys.stderr)
                    continue
                pool.submit(run_line, dict(line_params))
    except jsonlines.InvalidLineError as exc:
        print(f"ajl: invalid JSON Lines input: {exc}", file=sys.stderr)
        errors += 1
    finally:
        reader.close()
    return errors


def main(argv=None):
    """Entry point wrapper: exit cleanly on a closed pipe (head/wc) or Ctrl-C,
    the way a well-behaved Unix filter does, instead of dumping a traceback."""
    try:
        return _run(argv)
    except BrokenPipeError:
        # downstream closed the pipe; silence the interpreter's flush-on-exit
        with contextlib.suppress(Exception):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        print("ajl: interrupted", file=sys.stderr)
        return 130


def _run(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(flag in argv for flag in ("-h", "--help")):
        # route help to the subparser before the root parser eats it
        if argv[:2] in (["s3", "scan"], ["s3", "list"]):
            from .scan import build_list_parser, build_scan_parser

            (build_scan_parser() if argv[1] == "scan" else build_list_parser()).parse_args(["--help"])
        elif argv[:2] == ["ssm", "get"]:
            from .ssm import build_get_parser

            build_get_parser().parse_args(["--help"])
        elif argv[:2] in (["ssm", "update"], ["ssm", "put"]):
            from .ssm import build_write_parser

            build_write_parser(argv[1]).parse_args(["--help"])
    parser = build_parser()
    options, passthrough = parser.parse_known_args(argv)

    if options.version:
        print(runtime_version())
        return 0

    service = None
    operation = None
    if passthrough and not passthrough[0].startswith("-"):
        service = passthrough.pop(0)
    if passthrough and not passthrough[0].startswith("-"):
        operation = passthrough.pop(0)

    if service == "cache":
        return run_cache_command(operation, passthrough)
    if service == "decrypt":
        from .ssm import run_decrypt_filter

        return run_decrypt_filter(options)

    started = time.monotonic()
    learning = learn.enabled(options)
    learn_record = {
        "Command": "ajl " + " ".join(argv),
        "Service": service,
        "Operation": operation,
        "Profile": options.profile or DEFAULT_PROFILE,
        "Region": options.region or DEFAULT_REGION,
        "Cache": "off",
    }

    result_cache = ResultCache(options)
    SENSITIVE = {"ssm", "secretsmanager"}
    if result_cache.enabled and service in SENSITIVE:
        from .cache import encryption_mode

        if not encryption_mode():
            print(f"ajl: caching {service} requires encryption — no age identity "
                  "configured", file=sys.stderr)
            print(seal.setup_hint(), file=sys.stderr)
            print("ajl: or skip caching for this run: drop --cache", file=sys.stderr)
            return 2
    try:
        cache_key = None
        if result_cache.enabled:
            result_cache.buffer_params_stdin(options)
            cache_key = result_cache.key(options, service, operation, passthrough)
            meta = result_cache.try_replay(cache_key)
            if meta is not None:
                if learning:
                    learn.log({**learn_record, "Cache": "hit",
                               "DurationS": round(time.monotonic() - started, 3),
                               "SavedS": meta.get("duration"), "ExitCode": 0})
                return 0
            learn_record["Cache"] = "miss"

        if apilog.enabled(options):
            apilog.announce()
            api_logging = True
        else:
            api_logging = False
        runner = Runner(
            default_profile=options.profile or DEFAULT_PROFILE,
            default_region=options.region or DEFAULT_REGION,
            verbose=options.verbose,
            api_log=api_logging,
        )

        fanning = bool(options.all or options.all_profiles or options.all_regions
                       or options.profiles is not None or options.regions is not None)
        options.stamp_session = should_stamp_session(options, fanning)

        writer = result_cache.open_writer(cache_key, argv) if result_cache.enabled else None
        emitter = Emitter(stream=writer)
        if options.fetch_tags:
            emitter = TagMergeEmitter(
                emitter,
                lambda session_key: runner.client(session_key, "resourcegroupstaggingapi"),
            )
        if options.jq:
            emitter = JqEmitter(emitter, options.jq)
        if options.stamp_session:
            emitter = StampEmitter(emitter, runner)

        exit_code = 0
        report = {}
        try:
            if service == "s3" and operation in ("scan", "list"):
                from .scan import run_list, run_scan

                if learning:
                    learn.announce(learn_record["Command"])
                run = run_scan if operation == "scan" else run_list
                exit_code = run(runner, emitter, options, passthrough, report=report)
            elif service == "ssm" and operation == "get":
                from .ssm import run_get

                if learning:
                    learn.announce(learn_record["Command"])
                exit_code = run_get(runner, emitter, options, passthrough, report=report)
            elif service == "ssm" and operation in ("update", "put"):
                from .ssm import run_write

                if learning:
                    learn.announce(f"aws ssm put-parameter ({operation})")
                exit_code = run_write(runner, emitter, options, passthrough, operation,
                                      report=report)
            elif service == "ssm" and operation == "params":
                # alias: describe-parameters with adaptive throttle + cache
                if learning:
                    learn.announce("aws ssm describe-parameters")
                params = coerce_params(
                    parse_extra_options(passthrough), "ssm", "DescribeParameters")

                def describe(session_key):
                    run_operation(runner, emitter, options, "ssm", "describe-parameters",
                                  params, session_key)

                if fanning:
                    from .fanout import run_fanout
                    exit_code = run_fanout(runner, emitter, options, describe, "ssm")
                else:
                    try:
                        describe(runner.session_key())
                    except Exception as exc:
                        print(f"ajl: ssm.describe-parameters failed: {exc}", file=sys.stderr)
                        exit_code = 1
            elif options.params_json:
                extra_options = parse_extra_options(passthrough, verbose=options.verbose)
                if learning:
                    learn.announce(learn_record["Command"])
                errors = run_params_json(runner, emitter, options, service, operation, extra_options)
                exit_code = 1 if errors else 0
            else:
                if not service or not operation:
                    parser.error("a <service> and <operation> are required (or use --params-json)")
                extra_options = parse_extra_options(passthrough, verbose=options.verbose)
                params = coerce_params(extra_options, service, caseconverter.pascalcase(operation))
                learn_record["AwsEquivalent"] = learn.aws_equivalent(
                    service, operation, params,
                    profile=options.profile, region=options.region,
                )
                if learning:
                    learn.announce(learn_record["AwsEquivalent"])

                def one(session_key):
                    run_operation(runner, emitter, options, service, operation, params, session_key)

                if fanning:
                    from .fanout import run_fanout
                    exit_code = run_fanout(runner, emitter, options, one, service)
                else:
                    try:
                        one(runner.session_key())
                    except Exception as exc:
                        print(f"ajl: {service}.{operation} failed: {exc}", file=sys.stderr)
                        exit_code = 1
        except BaseException:
            # interrupt / broken pipe / error: discard any partial cache so a
            # killed run can never be replayed as if it were complete
            if writer is not None:
                writer.abort()
                writer = None
            raise
        finally:
            with contextlib.suppress(BrokenPipeError, ValueError):
                emitter.flush()
        # reached only on normal completion — cache only a clean (exit 0) run
        if writer is not None:
            if exit_code == 0:
                writer.commit(time.monotonic() - started)
            else:
                writer.abort()
        if learning:
            learn_record.update(report)
            learn.log({**learn_record, "ExitCode": exit_code,
                       "DurationS": round(time.monotonic() - started, 3)})
        return exit_code
    finally:
        result_cache.cleanup_spool()


if __name__ == "__main__":
    sys.exit(main())
