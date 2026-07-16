"""ajl CLI entry point.

Usage:
    ajl <service> <operation> [--param value ...] [options]
    ... | ajl --params-json - [defaults...] [options]

Operations and parameters are given aws-cli style in kebab-case and converted
to the PascalCase boto3 expects; parameter values are coerced using the
service model's input member types (integers, booleans, lists, JSON).
"""

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
import caseconverter
import jq as jqlib
import jsonlines
import orjson

from . import learn
from .cache import ResultCache, run_cache_command
from .debug import cache_hit
from .modelconfig import get_operation_config
from .normalize import iter_configured_resources, iter_default_resources
from .pagination import iter_pages
from .tags import TagMergeEmitter

DEFAULT_PROFILE = os.environ.get("AWS_PROFILE", os.environ.get("AWS_DEFAULT_PROFILE"))
DEFAULT_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ajl",
        description="Stream AWS API responses as JSON Lines with consistent "
        "Type/Id/Name/Arn/Tags properties.",
        allow_abbrev=False,
    )
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
    parser.add_argument("--jq", type=str, default=None, metavar="PROGRAM",
                        help="post-shaping jq filter applied to every record; empty "
                        "output drops the record, multiple outputs emit multiple lines")
    parser.add_argument("--stamp-session", action="store_true", default=False,
                        help="add Profile/Region/Account to every record so piped "
                        "--params-json stages reuse the same credentials")
    parser.add_argument("--workers", type=int, default=8,
                        help="parallel requests in --params-json mode (default 8; "
                        "use 1 to preserve input order)")
    parser.add_argument("--cache", type=str, default=None, metavar="TTL",
                        help="serve cached results younger than TTL (e.g. 15m, 2h) "
                        "and store fresh ones, gzipped (+age encrypted when "
                        "AJL_AGE_* is set) under ~/.cache/ajl; AJL_CACHE sets a "
                        "default; manage with 'ajl cache ls|clear|keygen'")
    parser.add_argument("--no-cache", action="store_true", default=False,
                        help="disable the result cache for this run, overriding "
                        "an AJL_CACHE default")
    parser.add_argument("--refresh", action="store_true", default=False,
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
    stage — with Type/Id/Name/Arn/Tags etc. — can be piped back in as-is)."""
    operation_cfg = get_operation_config(service, operation_pascal) or {}
    members = (operation_cfg.get("input") or {}).get("members") or {}
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
        with self.lock:
            self.stream.write(line + "\n")
            now = time.monotonic()
            if now - self._last_flush >= self.flush_interval:
                self.stream.flush()
                self._last_flush = now

    def flush(self):
        with self.lock:
            self.stream.flush()


class Runner:
    """Caches boto3 sessions/clients per (profile, region) for reuse and
    thread safety."""

    def __init__(self, default_profile=None, default_region=None, verbose=False):
        self.default_profile = default_profile
        self.default_region = default_region
        self.verbose = verbose
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
                self._clients[key] = self.session(session_key).client(service)
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

    emitted = 0
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
            emitter.emit(page, session_key)
            continue
        for record in shape_page(page, operation_cfg, context, runner, session_key):
            emitter.emit(record, session_key)
            emitted += 1
            if options.max_items and emitted >= options.max_items:
                return


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
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:2] in (["s3", "scan"], ["s3", "list"]) and any(
        flag in argv for flag in ("-h", "--help")
    ):
        # route help to the subparser before the root parser eats it
        from .scan import build_list_parser, build_scan_parser

        subparser = build_scan_parser() if argv[1] == "scan" else build_list_parser()
        subparser.parse_args(["--help"])
    parser = build_parser()
    options, passthrough = parser.parse_known_args(argv)

    service = None
    operation = None
    if passthrough and not passthrough[0].startswith("-"):
        service = passthrough.pop(0)
    if passthrough and not passthrough[0].startswith("-"):
        operation = passthrough.pop(0)

    if service == "cache":
        return run_cache_command(operation, passthrough)

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

        runner = Runner(
            default_profile=options.profile or DEFAULT_PROFILE,
            default_region=options.region or DEFAULT_REGION,
            verbose=options.verbose,
        )

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
                session_key = runner.session_key()
                params = coerce_params(extra_options, service, caseconverter.pascalcase(operation))
                learn_record["AwsEquivalent"] = learn.aws_equivalent(
                    service, operation, params,
                    profile=options.profile, region=options.region,
                )
                if learning:
                    learn.announce(learn_record["AwsEquivalent"])
                try:
                    run_operation(runner, emitter, options, service, operation, params, session_key)
                except Exception as exc:
                    print(f"ajl: {service}.{operation} failed: {exc}", file=sys.stderr)
                    exit_code = 1
        finally:
            emitter.flush()
            if writer:
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
