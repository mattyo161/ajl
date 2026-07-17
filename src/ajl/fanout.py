"""Multi-account / multi-region fan-out (--all-profiles / --all-regions / --all).

Automates the old ``{"Profile":"prod","Region":"us-east-1"}`` per-line pattern:
expand the invocation across a set of (profile, region) sessions and run each
on the worker pool, stamping every record with its origin.

Profiles come from ``AJL_PROFILES`` (comma/space list) if set, else the named
profiles in ``~/.aws/config``. Regions come from one ``describe-regions`` call
per account (enabled regions only, incl. opt-in) when --all-regions is set,
else the single resolved region. Profiles are de-duped by account id so two
profiles pointing at the same account don't double-scan. A session that
fails auth (expired SSO, no access to a region) becomes a stderr warning and
a stat, never a hard failure — you get everything you *can* reach.
"""

import configparser
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor


def resolve_profiles():
    env = os.environ.get("AJL_PROFILES", "")
    listed = [p for p in env.replace(",", " ").split() if p]
    if listed:
        return listed
    path = os.path.expanduser(os.environ.get("AWS_CONFIG_FILE", "~/.aws/config"))
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except OSError:
        return []
    profiles = []
    for section in parser.sections():
        # config sections are "[profile name]" except "[default]"
        if section == "default":
            profiles.append("default")
        elif section.startswith("profile "):
            profiles.append(section[len("profile "):])
    return profiles


def enabled_regions(runner, session_key):
    try:
        ec2 = runner.client(session_key, "ec2")
        response = ec2.describe_regions()  # enabled (opt-in included) by default
        return sorted(r["RegionName"] for r in response.get("Regions") or [])
    except Exception as exc:
        print(f"ajl: could not list regions for {session_key}: {exc}", file=sys.stderr)
        return []


def plan_sessions(runner, options):
    """Return a de-duped list of (profile, region) session keys to fan across."""
    want_profiles = options.all or options.all_profiles
    want_regions = options.all or options.all_regions
    profiles = resolve_profiles() if want_profiles else [options.profile]

    sessions = []
    seen_accounts = set()
    for profile in profiles:
        probe = runner.session_key(profile, options.region)
        account = runner.account(probe) if want_profiles else None
        if account:
            if account in seen_accounts:
                continue  # a second profile for an account already covered
            seen_accounts.add(account)
        regions = enabled_regions(runner, probe) if want_regions else [probe[1]]
        for region in regions:
            sessions.append(runner.session_key(profile, region))
    return sessions


def run_fanout(runner, emitter, options, run_one):
    """Run ``run_one(session_key)`` for every planned session, in parallel.
    ``run_one`` should raise on failure; auth/access errors are contained."""
    sessions = plan_sessions(runner, options)
    if not sessions:
        print("ajl: --all found no profiles/regions to fan out to", file=sys.stderr)
        return 1
    if options.verbose:
        print(f"ajl: fanning out across {len(sessions)} sessions", file=sys.stderr)
    errors = 0
    lock = threading.Lock()

    def do(session_key):
        nonlocal errors
        try:
            run_one(session_key)
        except Exception as exc:
            with lock:
                errors += 1
            print(f"ajl: {session_key} skipped: {exc}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=max(1, options.workers)) as pool:
        list(pool.map(do, sessions))
    return 1 if errors else 0
