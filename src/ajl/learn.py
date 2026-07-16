"""The learn log (the tiss learnExec idea, built in).

``--learn`` (or ``AJL_LEARN=1``) does two things per invocation:

- prints one ``[learn]`` line to stderr up front showing the aws-cli
  equivalent of what ajl is about to do — the teaching moment: juniors see
  the underlying command, seniors get a sanity check;
- appends a full JSONL record to ``AJL_LEARN_FILE`` (default
  ``~/.local/state/ajl/learn.jsonl``): argv, the aws equivalent, resolved
  profile/region, duration, exit code, cache status, and for scans the run
  stats plus a small sample of the keyspace slices actually listed (the
  interesting ``--prefix``/``StartAfter``/``EndAt`` jumps — never the
  per-page marker churn). One line per invocation makes it a lightweight
  audit and performance log too.
"""

import datetime
import os
import sys

import caseconverter
import orjson


def enabled(options):
    if getattr(options, "learn", False):
        return True
    return os.environ.get("AJL_LEARN", "").lower() in ("1", "true", "yes", "on")


def learn_file():
    return os.environ.get("AJL_LEARN_FILE") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
        "ajl", "learn.jsonl",
    )


def _render_value(value):
    if isinstance(value, bool):
        return None  # flags render bare
    if isinstance(value, (dict, list)):
        return "'" + orjson.dumps(value).decode() + "'"
    return str(value)


def aws_equivalent(service, operation, params, profile=None, region=None):
    """The aws-cli command that would make (one of) the same call(s)."""
    if not service or not operation:
        return None
    cli_service = {"elb": "elb", "elbv2": "elbv2", "s3": "s3api"}.get(service, service)
    parts = [f"aws {cli_service} {caseconverter.kebabcase(operation)}"]
    for key, value in (params or {}).items():
        flag = "--" + caseconverter.kebabcase(key)
        rendered = _render_value(value)
        parts.append(flag if rendered is None else f"{flag} {rendered}")
    if profile:
        parts.append(f"--profile {profile}")
    if region:
        parts.append(f"--region {region}")
    return " ".join(parts)


def announce(text):
    print(f"ajl: [learn] {text}", file=sys.stderr)


def log(record):
    path = learn_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = {"Ts": datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"), **record}
    with open(path, "a") as fp:
        fp.write(orjson.dumps(record).decode() + "\n")
