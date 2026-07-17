"""Multi-account / multi-region fan-out (--all-profiles / --all-regions / --all).

Automates the old ``{"Profile":"prod","Region":"us-east-1"}`` per-line pattern:
expand the invocation across a set of (profile, region) sessions and run each
on the worker pool, stamping every record with its origin.

Profiles come from ``AJL_PROFILES`` (comma/space list) if set, else the named
profiles in ``~/.aws/config``. Regions come from ``AJL_REGIONS`` if set, else
botocore's *static* region list for the service — no ``DescribeRegions`` call,
so it needs no ec2 permission and adds no latency (a region the account can't
use just fails that one session, contained as a warning). Profiles are
de-duped by account id (looked up in parallel); a profile whose credentials
are dead is skipped with a warning rather than fanned across every region.
Per-session failures never abort the run — you get everything you can reach.
"""

import configparser
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from tqdm import tqdm

# Regions disabled by default (require explicit account opt-in). Fanning into
# them without enablement just yields UnrecognizedClientException — or, worse,
# a 60s STS/endpoint connect-timeout hang. Excluded from --all-regions unless
# the user names regions explicitly via AJL_REGIONS.
OPT_IN_REGIONS = frozenset({
    "af-south-1", "ap-east-1", "ap-east-2", "ap-south-2", "ap-southeast-3",
    "ap-southeast-4", "ap-southeast-5", "ap-southeast-6", "ap-southeast-7",
    "ca-west-1", "eu-central-2", "eu-south-1", "eu-south-2", "il-central-1",
    "me-central-1", "me-south-1", "mx-central-1",
})


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
        if section == "default":
            profiles.append("default")
        elif section.startswith("profile "):
            profiles.append(section[len("profile "):])
    return profiles


def resolve_regions(runner, session_key, service):
    """Regions to fan across — AJL_REGIONS override, else botocore's static
    list for the service (no API call, no permissions)."""
    env = os.environ.get("AJL_REGIONS", "")
    listed = [r for r in env.replace(",", " ").split() if r]
    if listed:
        return listed  # explicit list wins, opt-in regions included if named
    try:
        regions = runner.session(session_key).get_available_regions(service)
    except Exception:
        regions = []
    # skip opt-in-by-default regions the account probably hasn't enabled
    regions = [r for r in regions if r not in OPT_IN_REGIONS]
    return sorted(regions) or [session_key[1]]


def plan_sessions(runner, options, service):
    """Return a de-duped list of (profile, region) session keys to fan across."""
    want_profiles = options.all or options.all_profiles
    want_regions = options.all or options.all_regions
    profiles = resolve_profiles() if want_profiles else [options.profile]

    # resolve accounts in parallel for dedup / dead-credential detection
    accounts = {}
    if want_profiles:
        def probe(profile):
            return profile, runner.account(runner.session_key(profile, options.region))
        with ThreadPoolExecutor(max_workers=min(16, len(profiles) or 1)) as pool:
            accounts = dict(pool.map(probe, profiles))

    sessions = []
    seen_accounts = set()
    for profile in profiles:
        if want_profiles:
            account = accounts.get(profile)
            if not account:
                print(f"ajl: skipping profile {profile!r} — could not resolve credentials",
                      file=sys.stderr)
                continue
            if account in seen_accounts:
                continue  # a second profile for an account already covered
            seen_accounts.add(account)
        base = runner.session_key(profile, options.region)
        regions = resolve_regions(runner, base, service) if want_regions else [base[1]]
        for region in regions:
            sessions.append(runner.session_key(profile, region))
    return sessions


def run_fanout(runner, emitter, options, run_one, service):
    """Run ``run_one(session_key)`` for every planned session, in parallel.
    ``run_one`` should raise on failure; per-session errors are contained."""
    sessions = plan_sessions(runner, options, service)
    if not sessions:
        print("ajl: --all found no profiles/regions to fan out to", file=sys.stderr)
        return 1
    profiles = len({s[0] for s in sessions})
    print(f"ajl: fanning out across {len(sessions)} sessions "
          f"({profiles} accounts x regions)", file=sys.stderr)

    errors = 0
    seen = {}  # error signature -> count; print each distinct error once
    lock = threading.Lock()
    tty = sys.stderr.isatty()
    show = tty and not getattr(options, "no_progress", False)
    bar = tqdm(total=len(sessions), desc="ajl fanout", unit=" session",
               file=sys.stderr, dynamic_ncols=True) if show else None

    def warn(msg):
        line = f"\033[33m{msg}\033[0m" if tty else msg  # yellow on a TTY
        if bar is not None:
            tqdm.write(line, file=sys.stderr)  # scrolls cleanly above the bar
        else:
            print(line, file=sys.stderr)

    def signature(exc):
        try:
            return exc.response["Error"]["Code"]  # botocore ClientError code
        except (AttributeError, KeyError, TypeError):
            return str(exc).splitlines()[0][:60]

    def do(session_key):
        nonlocal errors
        label = f"{session_key[0] or 'default'}/{session_key[1]}"
        try:
            run_one(session_key)
        except Exception as exc:
            sig = signature(exc)
            with lock:
                errors += 1
                first = sig not in seen
                seen[sig] = seen.get(sig, 0) + 1
            if first:  # only the first occurrence of each distinct error prints
                warn(f"ajl: {label} skipped: {str(exc).splitlines()[0][:140]}")
        finally:
            if bar is not None:
                with lock:
                    bar.update(1)
                    bar.set_postfix(errors=errors, refresh=False)

    pool = ThreadPoolExecutor(max_workers=max(1, options.workers))
    try:
        list(pool.map(do, sessions))
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        if bar is not None:
            bar.close()
        raise
    pool.shutdown(wait=True)
    if bar is not None:
        bar.close()
    ok = len(sessions) - errors
    if seen:  # collapse repeats: one summary line per distinct error, with counts
        breakdown = ", ".join(f"{sig}x{n}" for sig, n in sorted(seen.items(),
                                                                key=lambda kv: -kv[1]))
        warn(f"ajl: {errors} sessions skipped — {breakdown}")
    print(f"ajl: fanout done — {ok}/{len(sessions)} sessions ok", file=sys.stderr)
    # best-effort: a few unreachable regions/accounts shouldn't fail the run;
    # only error out if *nothing* was reachable
    return 0 if ok else 1
