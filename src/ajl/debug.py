"""Opt-in internal-cache diagnostics.

Set ``AJL_DEBUG_CACHE=1`` (or true/yes/on) to print a line to stderr whenever
ajl serves an internally cached object instead of building a fresh one: boto3
sessions and clients, STS account ids, compiled jq programs, service models,
and scan's s3 clients. Lines go to stderr so stdout stays pure JSONL.

(The *result* cache's hit notice is separate and always on — see cache.py —
because silently serving cached data would invite staleness confusion.)
"""

import os
import sys


def enabled():
    return os.environ.get("AJL_DEBUG_CACHE", "").lower() in ("1", "true", "yes", "on")


def cache_hit(kind, detail=""):
    if enabled():
        print(f"ajl: cache hit {kind}: {detail}", file=sys.stderr)
