"""Persistent cross-process cache for resolved AWS account ids, keyed by
profile name (``~/.local/state/ajl/accounts.json`` by default; override with
``AJL_ACCOUNT_CACHE_FILE``).

``Runner.account()`` already caches in-memory for the life of one invocation,
but ``tools/inventory.sh``-style fan-out spawns one short-lived ``ajl``
process per (profile, region) pair, and every process re-resolves every
profile's account id from scratch via ``sts.GetCallerIdentity`` even though
the same profile recurs across dozens of processes in one run. A real
51-session inventory run measured this at 2,979 ``GetCallerIdentity`` calls /
1,281.5s (14.5% of all cumulative API time) for a value that's effectively
immutable. This cache collapses that to one real call per profile per TTL
window (default 24h, override with ``AJL_ACCOUNT_CACHE_TTL`` in seconds).

Only named profiles are cached -- with no ``--profile``, credentials can come
from process-specific environment variables that vary invocation to
invocation even though the profile argument stays ``None``, so caching that
case under a bare ``None``/"" key would risk serving one process's account id
to a different process running under different credentials.
"""

import contextlib
import json
import os
import tempfile
import time

DEFAULT_TTL = 24 * 60 * 60


def cache_file():
    return os.environ.get("AJL_ACCOUNT_CACHE_FILE") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
        "ajl",
        "accounts.json",
    )


def _ttl():
    return float(os.environ.get("AJL_ACCOUNT_CACHE_TTL", DEFAULT_TTL))


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def get(profile):
    """Return a cached account id for ``profile``, or None on miss/expiry."""
    if not profile:
        return None
    entry = _load(cache_file()).get(profile)
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > _ttl():
        return None
    return entry.get("account") or None


def put(profile, account):
    """Record ``account`` as the resolved id for ``profile``."""
    if not profile or not account:
        return
    path = cache_file()
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    data = _load(path)
    data[profile] = {"account": account, "ts": time.time()}
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".accounts-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
