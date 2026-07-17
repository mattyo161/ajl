"""Per-API-call telemetry log (``--api-log`` / ``AJL_APILOG=1``).

Every botocore call made through a ``Runner`` client gets one JSONL record in
``AJL_APILOG_FILE`` (default ``~/.local/state/ajl/apilog.jsonl``): service,
operation, profile/region, duration, HTTP status, retry attempts, item count,
and success/error outcome.

Hooked in via botocore's ``before-call``/``after-call``/``after-call-error``
events, registered once per client in ``Runner.client()``. botocore's event
emitter is hierarchical: a handler registered on ``before-call`` fires for
every ``before-call.<service>.<operation>`` emitted on that client, so one
registration catches every operation the client makes.

Each boto3 client gets its own independent copy of the event emitter (checked
against botocore 1.40), so this only sees calls made through clients ajl
itself builds via ``Runner.client()``. It does NOT see credential-resolution
sub-clients boto3 builds internally (SSO ``GetRoleCredentials``, an
``AssumeRole`` fetcher) — those run on their own emitter copies. A missing
call in this log on an otherwise-slow request is itself informative: it
narrows the wait to credential resolution rather than the logged call.
"""

import datetime
import os
import sys
import threading
import time

import orjson

_write_lock = threading.Lock()


def enabled(options):
    if getattr(options, "no_api_log", False):
        return False
    if getattr(options, "api_log", False):
        return True
    return os.environ.get("AJL_APILOG", "").lower() in ("1", "true", "yes", "on")


def log_file():
    return os.environ.get("AJL_APILOG_FILE") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
        "ajl",
        "apilog.jsonl",
    )


def announce():
    print(f"ajl: [api-log] appending to {log_file()}", file=sys.stderr)


def _append(record):
    path = log_file()
    record = {
        "Ts": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="milliseconds"
        ),
        **record,
    }
    line = orjson.dumps(record).decode() + "\n"
    with _write_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fp:
            fp.write(line)


def _item_count(parsed):
    """Best-effort page size: total length of the response's list-typed
    top-level fields (Reservations, Parameters, Contents, ...)."""
    if not isinstance(parsed, dict):
        return None
    lists = [value for value in parsed.values() if isinstance(value, list)]
    return sum(len(value) for value in lists) if lists else None


def _error_code(exception):
    response = getattr(exception, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code") or type(exception).__name__
    return type(exception).__name__


def attach(client, service, profile, region):
    """Register before/after-call handlers on one client so every call it
    makes — including its own credential-resolution calls — gets logged."""

    def before_call(context, **kwargs):
        context["ajl_apilog_started"] = time.monotonic()

    def after_call(http_response, parsed, model, context, **kwargs):
        started = context.pop("ajl_apilog_started", None)
        metadata = (parsed or {}).get("ResponseMetadata", {})
        _append(
            {
                "Service": service,
                "Operation": model.name,
                "Profile": profile or "",
                "Region": region or "",
                "DurationS": round(time.monotonic() - started, 3) if started else None,
                "HttpStatus": getattr(http_response, "status_code", None)
                or metadata.get("HTTPStatusCode"),
                "Attempts": metadata.get("RetryAttempts", 0) + 1,
                "Items": _item_count(parsed),
                "Outcome": "success",
            }
        )

    def after_call_error(exception, context, event_name, **kwargs):
        started = context.pop("ajl_apilog_started", None)
        operation = event_name.split(".", 2)[-1] if event_name else ""
        _append(
            {
                "Service": service,
                "Operation": operation,
                "Profile": profile or "",
                "Region": region or "",
                "DurationS": round(time.monotonic() - started, 3) if started else None,
                "HttpStatus": None,
                "Attempts": (context.get("retries") or {}).get("attempt", 1),
                "Items": None,
                "Outcome": "error",
                "ErrorCode": _error_code(exception),
            }
        )

    events = client.meta.events
    events.register("before-call", before_call)
    events.register("after-call", after_call)
    events.register("after-call-error", after_call_error)
