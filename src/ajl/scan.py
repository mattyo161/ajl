"""Orchestrated S3 inventory fan-out (``ajl s3 scan``).

One bounded worker pool pulls listing tasks from a shared queue. Each task is
one ``list-objects-v2`` listing over a ``(bucket, prefix, start_after,
end_at)`` slice of the keyspace; ``Contents`` stream to stdout as
``s3:object`` records and ``CommonPrefixes`` go back into the queue with the
remaining delimiter schedule. Ranges use exclusive ``start_after`` and
inclusive ``end_at`` bounds so adjacent slices partition the keyspace with no
gap and no overlap.

Per-task decision loop:

- listing fits in one page              -> emit, done (one call, the minimum)
- delimiter fan                         -> enqueue child prefixes, keep paging
- fan exceeds ``--max-fan``             -> abandon the delimiter; requeue the
                                           remainder as a range for the splitter
- schedule exhausted and still paging
  after ``--split-after`` pages         -> ask the splitter to carve the rest
                                           of the range into parallel slices

Splitters implement ``split(ctx) -> list[{"start_after", "end_at"}] | None``
(``None`` = cannot split, keep paginating; ``ctx`` is a :class:`SplitContext`).
The default :class:`RadixSplitter` discovers the live key alphabet with
StartAfter leapfrog probes, descending single-branch levels so a long shared
hash prefix costs probes instead of serial pages. Fixed splitters (``hex2``,
``hex3``, ``alnum2``, ``b64-2``) cut at precomputed boundaries; drop-in
strategies load via ``--split-class package.module:ClassName``.

Failed tasks are written to ``--failed-out`` as seed lines (with StartAfter
advanced to the last emitted key) so a re-run scans only what's missing.
"""

import argparse
import importlib
import itertools
import re
import string
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, replace

import jsonlines
import orjson
from botocore.config import Config

PAGE_SIZE = 1000
_MAX_CHAR = "\U0010ffff"  # highest code point; UTF-8 F4 8F BF BF sorts last


def sentinel_after(prefix):
    """A string sorting after every S3 key that starts with ``prefix``
    (keys are at most 1024 bytes)."""
    reps = max(1, (1024 - len(prefix.encode())) // 4 + 1)
    return prefix + _MAX_CHAR * reps


@dataclass
class Task:
    """One listing over a slice of a bucket's keyspace.

    ``start_after`` is exclusive, ``end_at`` inclusive: a task emits keys k
    with ``start_after < k <= end_at``.
    """

    bucket: str
    prefix: str = ""
    delimiters: tuple = ()
    start_after: str = ""
    end_at: str = ""
    profile: str = None
    region: str = None
    no_split: bool = False

    def seed(self):
        """The task as a --params-json / --failed-out JSONL line."""
        out = {"Bucket": self.bucket, "Prefix": self.prefix}
        if self.delimiters:
            out["Delimiters"] = list(self.delimiters)
        if self.start_after:
            out["StartAfter"] = self.start_after
        if self.end_at:
            out["EndAt"] = self.end_at
        if self.profile:
            out["profile"] = self.profile
        if self.region:
            out["region"] = self.region
        return out


@dataclass
class SplitContext:
    """What a splitter gets to work with. ``last_key`` is the greatest key
    already emitted; a splitter's ranges must cover (last_key, end_at]."""

    client: object
    bucket: str
    prefix: str
    last_key: str
    end_at: str
    count_call: object  # zero-arg callable; splitters call it per API request


class RadixSplitter:
    """Split a range by the key alphabet actually in use.

    Discovers branch characters with StartAfter leapfrog probes: MaxKeys=1
    finds the first key of a branch, then StartAfter jumps to a sentinel just
    past that branch to land on the next one. Levels with a single branch
    (a long shared hash prefix) extend the probe base and repeat, so skew
    costs a couple of probes per character instead of serial pages.
    """

    name = "radix"

    def __init__(self, max_branches=64, max_depth=48):
        self.max_branches = max_branches
        self.max_depth = max_depth

    def split(self, ctx):
        base = ctx.prefix
        chars = []
        for _ in range(self.max_depth):
            chars = self._branches(ctx, base)
            if len(chars) == 1:
                base += chars[0]
                continue
            break
        if len(chars) < 2:
            return None
        boundaries = [base + c for c in chars[1:]]
        starts = [ctx.last_key] + boundaries
        ends = boundaries + [ctx.end_at]
        return [{"start_after": s, "end_at": e} for s, e in zip(starts, ends)]

    def _branches(self, ctx, base):
        chars = []
        cursor = ctx.last_key
        while len(chars) < self.max_branches:
            ctx.count_call()
            response = ctx.client.list_objects_v2(
                Bucket=ctx.bucket, Prefix=base, StartAfter=cursor, MaxKeys=1
            )
            contents = response.get("Contents") or []
            if not contents:
                break
            key = contents[0]["Key"]
            if ctx.end_at and key > ctx.end_at:
                break
            if len(key) <= len(base):
                cursor = key  # a key equal to the base itself; ranges cover it
                continue
            chars.append(key[len(base)])
            cursor = sentinel_after(base + key[len(base)])
        return chars


class FixedRangeSplitter:
    """Split at precomputed boundaries (e.g. hex2 = 00..ff under the prefix).

    One-shot: boundaries sit at the task's prefix root, so a re-split deeper
    inside one of the ranges finds no usable boundaries and returns None
    (the range then pages serially). Use radix when the keyspace is skewed.
    """

    def __init__(self, name, alphabet, width):
        self.name = name
        self.suffixes = ["".join(t) for t in itertools.product(sorted(alphabet), repeat=width)]

    def split(self, ctx):
        boundaries = [
            b
            for b in (ctx.prefix + s for s in self.suffixes)
            if b > ctx.last_key and (not ctx.end_at or b < ctx.end_at)
        ]
        if not boundaries:
            return None
        starts = [ctx.last_key] + boundaries
        ends = boundaries + [ctx.end_at]
        return [{"start_after": s, "end_at": e} for s, e in zip(starts, ends)]


def builtin_splitters():
    hex_digits = "0123456789abcdef"
    alnum = string.digits + string.ascii_lowercase
    b64 = "".join(sorted(string.ascii_letters + string.digits + "+/"))
    return {
        "radix": lambda: RadixSplitter(),
        "hex2": lambda: FixedRangeSplitter("hex2", hex_digits, 2),
        "hex3": lambda: FixedRangeSplitter("hex3", hex_digits, 3),
        "alnum2": lambda: FixedRangeSplitter("alnum2", alnum, 2),
        "b64-2": lambda: FixedRangeSplitter("b64-2", b64, 2),
        "none": lambda: None,
    }


def load_splitter(spec):
    """Instantiate a drop-in splitter from 'package.module:ClassName'."""
    module_name, _, class_name = spec.replace(":", ".").rpartition(".")
    if not module_name:
        raise ValueError(f"--split-class {spec!r} must be 'package.module:ClassName'")
    cls = getattr(importlib.import_module(module_name), class_name)
    return cls()


class Scanner:
    """Bounded worker pool draining a queue of listing tasks."""

    def __init__(
        self,
        runner,
        emitter,
        splitter=None,
        split_after=10,
        max_fan=2000,
        page_size=PAGE_SIZE,
        max_items=None,
        emit_prefixes=False,
        failed_out=None,
        client_factory=None,
        workers=8,
        verbose=False,
    ):
        self.runner = runner
        self.emitter = emitter
        self.splitter = splitter
        self.split_after = max(1, split_after)
        self.max_fan = max_fan
        self.page_size = page_size
        self.max_items = max_items
        self.emit_prefixes = emit_prefixes
        self.failed_out = failed_out
        self.client_factory = client_factory
        self.workers = max(1, workers)
        self.verbose = verbose
        self.queue = deque()
        self.cond = threading.Condition()
        self.active = 0
        self.stopped = False
        self.emitted = 0
        self.stats = {
            "tasks": 0, "calls": 0, "objects": 0, "prefixes": 0,
            "splits": 0, "abandons": 0, "failures": 0,
        }
        self._clients = {}
        self._client_lock = threading.Lock()
        self._failed_lock = threading.Lock()

    def run(self, seeds):
        self.queue.extend(seeds)
        threads = [
            threading.Thread(target=self._worker, daemon=True) for _ in range(self.workers)
        ]
        stop_monitor = threading.Event()
        if self.verbose:
            threading.Thread(
                target=self._monitor, args=(stop_monitor,), daemon=True
            ).start()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        stop_monitor.set()
        return self.stats

    def add(self, task):
        with self.cond:
            self.queue.append(task)
            self.cond.notify()

    def client(self, session_key):
        with self._client_lock:
            client = self._clients.get(session_key)
            if client is None:
                if self.client_factory:
                    client = self.client_factory(session_key)
                else:
                    config = Config(
                        retries={"mode": "adaptive", "max_attempts": 10},
                        max_pool_connections=max(10, self.workers),
                    )
                    client = self.runner.session(session_key).client("s3", config=config)
                self._clients[session_key] = client
            return client

    def _worker(self):
        while True:
            with self.cond:
                while not self.queue and self.active and not self.stopped:
                    self.cond.wait()
                if self.stopped or not self.queue:
                    self.cond.notify_all()
                    return
                task = self.queue.popleft()
                self.active += 1
            try:
                self._process(task)
            except Exception as exc:
                self._fail(task, exc)
            finally:
                with self.cond:
                    self.active -= 1
                    self.cond.notify_all()

    def _process(self, task):
        self._inc("tasks")
        session_key = self.runner.session_key(task.profile, task.region)
        client = self.client(session_key)
        partition = getattr(client.meta, "partition", "aws")
        delimiter = task.delimiters[0] if task.delimiters else None
        rest = tuple(task.delimiters[1:])
        params = {"Bucket": task.bucket, "MaxKeys": self.page_size}
        if task.prefix:
            params["Prefix"] = task.prefix
        if delimiter:
            params["Delimiter"] = delimiter
        if task.start_after:
            params["StartAfter"] = task.start_after
        progress = task.start_after
        token = None
        pages = 0
        fan = 0
        try:
            while not self.stopped:
                call_params = dict(params)
                if token:
                    call_params["ContinuationToken"] = token
                self._inc("calls")
                response = client.list_objects_v2(**call_params)
                pages += 1
                for item in response.get("Contents") or []:
                    key = item["Key"]
                    if task.end_at and key > task.end_at:
                        return
                    if not self._emit_object(item, task, partition, session_key):
                        return
                    progress = key
                if delimiter:
                    for cp in response.get("CommonPrefixes") or []:
                        child = cp["Prefix"]
                        self.add(
                            replace(task, prefix=child, delimiters=rest,
                                    start_after="", end_at="")
                        )
                        self._inc("prefixes")
                        fan += 1
                        progress = max(progress, sentinel_after(child))
                        if self.emit_prefixes:
                            self._emit_prefix(child, task, rest, partition, session_key)
                if not response.get("IsTruncated"):
                    return
                token = response.get("NextContinuationToken")
                if delimiter and fan > self.max_fan:
                    # over-shattered: everything <= progress is handled; hand
                    # the rest of the keyspace to the splitter path
                    self._inc("abandons")
                    if self.verbose:
                        print(
                            f"ajl: scan fan {fan} > {self.max_fan} under "
                            f"s3://{task.bucket}/{task.prefix}; switching to ranges",
                            file=sys.stderr,
                        )
                    self.add(
                        replace(task, delimiters=(), start_after=progress)
                    )
                    return
                if (
                    not delimiter
                    and not task.no_split
                    and self.splitter
                    and pages >= self.split_after
                ):
                    if self._split(task, client, progress):
                        return
                    task.no_split = True
        except Exception:
            task.start_after = progress  # resume point for the failure record
            raise

    def _split(self, task, client, progress):
        ctx = SplitContext(
            client=client,
            bucket=task.bucket,
            prefix=task.prefix,
            last_key=progress,
            end_at=task.end_at,
            count_call=lambda: self._inc("calls"),
        )
        ranges = self.splitter.split(ctx)
        if not ranges:
            return False
        for rng in ranges:
            self.add(
                replace(
                    task,
                    start_after=rng.get("start_after") or "",
                    end_at=rng.get("end_at") or "",
                    no_split=False,
                )
            )
        self._inc("splits")
        if self.verbose:
            print(
                f"ajl: scan split s3://{task.bucket}/{task.prefix} after "
                f"{progress!r} into {len(ranges)} ranges", file=sys.stderr,
            )
        return True

    def _count_emit(self):
        with self.cond:
            if self.max_items and self.emitted >= self.max_items:
                self.stopped = True
                self.cond.notify_all()
                return False
            self.emitted += 1
            return True

    def _emit_object(self, item, task, partition, session_key):
        if not self._count_emit():
            return False
        key = item["Key"]
        record = {
            "Type": "s3:object",
            "Id": key,
            "Name": key,
            "Arn": f"arn:{partition}:s3:::{task.bucket}/{key}",
            "Tags": {},
            "Uri": f"s3://{task.bucket}/{key}",
            "Bucket": task.bucket,
        }
        record.update(item)
        self.emitter.emit(record, session_key)
        self._inc("objects")
        return True

    def _emit_prefix(self, prefix, task, rest, partition, session_key):
        if not self._count_emit():
            return
        self.emitter.emit(
            {
                "Type": "s3:prefix",
                "Id": prefix,
                "Name": prefix,
                "Arn": f"arn:{partition}:s3:::{task.bucket}/{prefix}",
                "Tags": {},
                "Uri": f"s3://{task.bucket}/{prefix}",
                "Bucket": task.bucket,
                "Prefix": prefix,
                "Delimiter": rest[0] if rest else None,
            },
            session_key,
        )

    def _fail(self, task, exc):
        self._inc("failures")
        print(
            f"ajl: scan task failed (s3://{task.bucket}/{task.prefix} "
            f"after {task.start_after!r}): {exc}",
            file=sys.stderr,
        )
        if self.failed_out:
            line = orjson.dumps(task.seed()).decode()
            with self._failed_lock:
                self.failed_out.write(line + "\n")
                self.failed_out.flush()

    def _inc(self, stat):
        with self.cond:
            self.stats[stat] += 1

    def _monitor(self, stop):
        while not stop.wait(5):
            with self.cond:
                line = " ".join(f"{k}={v}" for k, v in self.stats.items())
                queued = len(self.queue)
            print(f"ajl: scan progress {line} queued={queued}", file=sys.stderr)


def parse_uri(uri):
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// uri: {uri!r}")
    bucket, _, prefix = uri[5:].partition("/")
    if not bucket:
        raise ValueError(f"missing bucket in uri: {uri!r}")
    return bucket, prefix


def seed_task(line, default_delimiters):
    """Build a Task from a --params-json line (accepts records emitted by
    ajl itself: Bucket/Prefix or Uri, plus optional StartAfter/EndAt and
    profile/region in either case)."""
    bucket = line.get("Bucket") or line.get("bucket")
    prefix = line.get("Prefix") or line.get("prefix") or ""
    uri = line.get("Uri") or line.get("uri")
    if not bucket and uri:
        bucket, prefix = parse_uri(uri)
    if not bucket:
        raise ValueError("line has no 'Bucket' or 'Uri'")
    end_at = line.get("EndAt") or ""
    delimiters = tuple(line.get("Delimiters") or default_delimiters)
    if end_at:
        delimiters = ()  # ranges are always leaf-mode listings
    return Task(
        bucket=bucket,
        prefix=prefix,
        delimiters=delimiters,
        start_after=line.get("StartAfter") or "",
        end_at=end_at,
        profile=line.get("profile") or line.get("Profile"),
        region=line.get("region") or line.get("Region"),
    )


def build_scan_parser():
    parser = argparse.ArgumentParser(
        prog="ajl s3 scan",
        description="Inventory buckets/prefixes with a delimiter fan-out "
        "worker pool and adaptive range splitting.",
        epilog="The global ajl flags also apply: --workers (pool size), "
        "--profile/--region, --max-items, --jq, --stamp-session, --fetch-tags, "
        "--params-json (seed tasks from JSONL) and --verbose (progress lines).",
    )
    parser.add_argument("uris", nargs="*", metavar="s3://bucket/prefix")
    parser.add_argument("--delimiters", default="", metavar="'/ / .'",
                        help="delimiter schedule, one level per entry "
                        "(whitespace or comma separated)")
    parser.add_argument("--split", default="radix", choices=sorted(builtin_splitters()),
                        help="range splitter for hot prefixes once the schedule "
                        "is exhausted (default: radix)")
    parser.add_argument("--split-class", default=None, metavar="pkg.mod:Class",
                        help="drop-in splitter class (overrides --split); must "
                        "implement split(ctx) -> [{'start_after','end_at'}] or None")
    parser.add_argument("--split-after", type=int, default=10, metavar="PAGES",
                        help="pages a task lists serially before asking the "
                        "splitter to fan it out (default 10)")
    parser.add_argument("--max-fan", type=int, default=2000, metavar="N",
                        help="max child prefixes from one delimiter listing "
                        "before switching to range splitting (default 2000)")
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE, metavar="N",
                        help="MaxKeys per request (default 1000)")
    parser.add_argument("--emit-prefixes", action="store_true", default=False,
                        help="also emit s3:prefix records for discovered prefixes")
    parser.add_argument("--failed-out", default=None, metavar="FILE",
                        help="write failed tasks as JSONL seeds for re-running "
                        "via --params-json")
    return parser


def run_scan(runner, emitter, options, tokens):
    """Entry point from main(); tokens are the args after 'ajl s3 scan'."""
    scan_options = build_scan_parser().parse_args(tokens)
    delimiters = tuple(d for d in re.split(r"[,\s]+", scan_options.delimiters) if d)

    seeds = []
    for uri in scan_options.uris:
        bucket, prefix = parse_uri(uri)
        seeds.append(Task(bucket=bucket, prefix=prefix, delimiters=delimiters))
    if options.params_json:
        source = sys.stdin if options.params_json == "-" else open(options.params_json)
        with jsonlines.Reader(source) as reader:
            for line in reader:
                try:
                    if not isinstance(line, dict):
                        raise ValueError("not a JSON object")
                    seeds.append(seed_task(line, delimiters))
                except ValueError as exc:
                    print(f"ajl: scan skipping seed line: {exc}", file=sys.stderr)
    if not seeds:
        print("ajl: s3 scan needs s3://bucket[/prefix] uris or --params-json",
              file=sys.stderr)
        return 2

    if scan_options.split_class:
        splitter = load_splitter(scan_options.split_class)
    else:
        splitter = builtin_splitters()[scan_options.split]()

    failed_fp = open(scan_options.failed_out, "w") if scan_options.failed_out else None
    scanner = Scanner(
        runner,
        emitter,
        splitter=splitter,
        split_after=scan_options.split_after,
        max_fan=scan_options.max_fan,
        page_size=scan_options.page_size,
        max_items=options.max_items,
        emit_prefixes=scan_options.emit_prefixes,
        failed_out=failed_fp,
        workers=options.workers,
        verbose=options.verbose,
    )
    started = time.time()
    try:
        stats = scanner.run(seeds)
    finally:
        if failed_fp:
            failed_fp.close()
    summary = " ".join(f"{k}={v}" for k, v in stats.items())
    print(f"ajl: scan done in {time.time() - started:.1f}s {summary}", file=sys.stderr)
    return 1 if stats["failures"] else 0
