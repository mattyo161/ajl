"""S3 keyspace listing engine: ``ajl s3 scan`` (recursive inventory fan-out)
and ``ajl s3 list`` (composable single-level list-objects-v2).

One bounded worker pool pulls listing tasks from a shared queue. Each task is
one ``list-objects-v2`` listing over a ``(bucket, prefix, start_after,
end_at)`` slice of the keyspace; ``Contents`` stream to stdout as
``s3:object`` records and ``CommonPrefixes`` either go back into the queue
with the remaining delimiter schedule (``scan``) or are emitted as pipeable
``s3:prefix`` records (``list``). Ranges use exclusive ``start_after`` and
inclusive ``end_at`` bounds so adjacent slices partition the keyspace with no
gap and no overlap.

Records are lean by design — at inventory volumes the Id/Name/Arn/Tags
contract properties are repetition: ``Uri`` (``s3://bucket/key``) carries the
identity, ``Tags`` exists only under ``--include-tags`` (one GetObjectTagging
call per object). The generic ``ajl s3 list-objects-v2`` shapes keep the full
five-property contract.

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
import random
import re
import string
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, replace
from urllib.parse import quote, unquote, urlsplit

import jsonlines
import orjson
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config

from .debug import cache_hit
from .normalize import tags_to_map

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

    def __init__(self, max_branches=64, max_depth=48, fan_target=256):
        self.max_branches = max_branches
        self.max_depth = max_depth
        self.fan_target = fan_target

    def split(self, ctx):
        base = self._descend(ctx)
        if base is None:
            return None
        chars = []
        for _ in range(self.max_depth):
            chars = self._branches(ctx, base)
            if len(chars) == 1:
                base += chars[0]
                continue
            break
        if len(chars) < 2:
            return None
        # extrapolate the discovered alphabet one level deeper without any
        # extra probes: ranges partition the keyspace no matter where the
        # boundaries sit, so a wrong guess only costs some empty ranges,
        # while a right one (uuids, hashes) fans len(chars)^2 wide at once
        suffixes = list(chars)
        if len(chars) ** 2 <= self.fan_target:
            suffixes = [c1 + c2 for c1 in chars for c2 in chars]
        boundaries = [base + s for s in suffixes]
        boundaries = [
            b for b in boundaries
            if b > ctx.last_key and (not ctx.end_at or b < ctx.end_at)
        ]
        if not boundaries:
            return None
        starts = [ctx.last_key] + boundaries
        ends = boundaries + [ctx.end_at]
        return [{"start_after": s, "end_at": e} for s, e in zip(starts, ends)]

    def _descend(self, ctx):
        """Binary-search the deepest prefix shared by every remaining key.

        Long literal prefixes (nrn:..., date paths, hash stems) would cost two
        probes per character walked one level at a time; instead probe the
        first remaining key, then bisect on 'does anything exist past
        sentinel(first[:depth])' — ~log2(keylen) calls total.
        """
        first = self._first_key(ctx, ctx.prefix, ctx.last_key)
        if first is None:
            return None
        low = len(ctx.prefix)  # all keys share ctx.prefix by construction
        high = len(first)  # nothing longer than the first key can be shared
        while low < high:
            mid = (low + high + 1) // 2
            if self._first_key(ctx, ctx.prefix, sentinel_after(first[:mid])) is None:
                low = mid  # everything starts with first[:mid]; go deeper
            else:
                high = mid - 1
        return first[:low]

    def _first_key(self, ctx, prefix, start_after):
        ctx.count_call()
        response = ctx.client.list_objects_v2(
            Bucket=ctx.bucket, Prefix=prefix, StartAfter=start_after, MaxKeys=1
        )
        contents = response.get("Contents") or []
        if not contents:
            return None
        key = contents[0]["Key"]
        if ctx.end_at and key > ctx.end_at:
            return None
        return key

    def _branches(self, ctx, base):
        chars = []
        cursor = ctx.last_key
        while len(chars) < self.max_branches:
            key = self._first_key(ctx, base, cursor)
            if key is None:
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


_S3_XMLNS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_RETRY_STATUSES = frozenset((429, 500, 502, 503, 504))


def parse_list_xml(body):
    """Parse a ListObjectsV2 response requested with encoding-type=url.

    Returns the boto3 response shape, except LastModified stays the ISO
    string S3 sent — botocore's tz-aware datetime parsing is ~100ms of GIL
    per 1000-key page, and ajl serializes it straight back to a string.
    """
    root = ET.fromstring(body)

    def text(name, default=""):
        return root.findtext(f"{_S3_XMLNS}{name}") or default

    contents = []
    for node in root.iter(f"{_S3_XMLNS}Contents"):
        item = {"Key": unquote(node.findtext(f"{_S3_XMLNS}Key") or "")}
        last_modified = node.findtext(f"{_S3_XMLNS}LastModified")
        if last_modified:
            item["LastModified"] = last_modified
        etag = node.findtext(f"{_S3_XMLNS}ETag")
        if etag:
            item["ETag"] = etag
        size = node.findtext(f"{_S3_XMLNS}Size")
        if size is not None:
            item["Size"] = int(size)
        storage_class = node.findtext(f"{_S3_XMLNS}StorageClass")
        if storage_class:
            item["StorageClass"] = storage_class
        contents.append(item)
    prefixes = [
        {"Prefix": unquote(node.findtext(f"{_S3_XMLNS}Prefix") or "")}
        for node in root.iter(f"{_S3_XMLNS}CommonPrefixes")
    ]
    response = {
        "Name": text("Name"),
        "KeyCount": int(text("KeyCount", "0") or 0),
        "IsTruncated": text("IsTruncated") == "true",
        "Contents": contents,
        "CommonPrefixes": prefixes,
    }
    token = text("NextContinuationToken")
    if token:
        response["NextContinuationToken"] = token
    return response


class FastLister:
    """ListObjectsV2 over raw SigV4-signed HTTP with C ElementTree parsing.

    Exists because botocore's generic response parser costs ~100ms of
    GIL-holding CPU per 1000-key page, which caps a whole worker pool at
    ~10 pages/s regardless of concurrency. This path parses a page in ~3ms.
    get_object_tagging (and anything else) stays on the boto3 client.
    """

    def __init__(self, boto_client, session, pool_size=32, max_attempts=10):
        self.credentials = session.get_credentials()
        self.region = boto_client.meta.region_name or "us-east-1"
        self.endpoint_url = boto_client.meta.endpoint_url
        self.max_attempts = max_attempts
        self.boto_client = boto_client
        self.meta = boto_client.meta
        self.http = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=pool_size, pool_maxsize=pool_size
        )
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)

    def get_object_tagging(self, **kwargs):
        return self.boto_client.get_object_tagging(**kwargs)

    def _base_url(self, bucket):
        host = urlsplit(self.endpoint_url).hostname or ""
        if host.endswith("amazonaws.com") and "." not in bucket:
            return f"https://{bucket}.s3.{self.region}.amazonaws.com/"
        # path-style for custom endpoints and dotted bucket names
        return f"{self.endpoint_url.rstrip('/')}/{bucket}/"

    def list_objects_v2(self, Bucket, Prefix=None, Delimiter=None, StartAfter=None,
                        ContinuationToken=None, MaxKeys=1000, **_):
        params = [("list-type", "2"), ("encoding-type", "url"),
                  ("max-keys", str(MaxKeys))]
        if ContinuationToken:
            params.append(("continuation-token", ContinuationToken))
        if Delimiter:
            params.append(("delimiter", Delimiter))
        if Prefix:
            params.append(("prefix", Prefix))
        if StartAfter:
            params.append(("start-after", StartAfter))
        params.sort()  # canonical order: sign and send the identical string
        query = "&".join(f"{k}={quote(v, safe='')}" for k, v in params)
        url = f"{self._base_url(Bucket)}?{query}"
        last_error = None
        for attempt in range(self.max_attempts):
            if attempt:
                time.sleep(min(0.05 * 2**attempt, 5) * (0.5 + random.random()))
            request = AWSRequest(method="GET", url=url)
            S3SigV4Auth(
                self.credentials.get_frozen_credentials(), "s3", self.region
            ).add_auth(request)
            try:
                response = self.http.get(
                    url, headers=dict(request.headers), timeout=(10, 120)
                )
            except requests.RequestException as exc:
                last_error = exc
                continue
            if response.status_code == 200:
                return parse_list_xml(response.content)
            last_error = RuntimeError(
                f"S3 returned {response.status_code}: {response.text[:300]}"
            )
            if response.status_code not in _RETRY_STATUSES:
                break
        raise last_error


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
        recurse=True,
        include_tags=False,
        failed_out=None,
        client_factory=None,
        workers=8,
        verbose=False,
        progress=False,
        fast=True,
        name="scan",
    ):
        self.runner = runner
        self.emitter = emitter
        self.splitter = splitter
        self.split_after = max(1, split_after)
        self.max_fan = max_fan
        self.page_size = page_size
        self.max_items = max_items
        self.emit_prefixes = emit_prefixes
        self.recurse = recurse  # False: emit CommonPrefixes instead of enqueueing
        self.include_tags = include_tags
        self.failed_out = failed_out
        self.client_factory = client_factory
        self.workers = max(1, workers)
        self.verbose = verbose
        self.progress = progress
        self.fast = fast
        self.name = name
        self.queue = deque()
        self.cond = threading.Condition()
        self.active = 0
        self.stopped = False
        self.emitted = 0
        self.stats = {
            "tasks": 0, "calls": 0, "objects": 0, "prefixes": 0,
            "emitted_prefixes": 0, "splits": 0, "abandons": 0,
            "failures": 0, "tag_errors": 0,
        }
        self.samples = []  # first few task slices, for the learn log
        self._clients = {}
        self._client_lock = threading.Lock()
        self._failed_lock = threading.Lock()

    def run(self, seeds):
        self.queue.extend(seeds)
        threads = [
            threading.Thread(target=self._worker, daemon=True) for _ in range(self.workers)
        ]
        stop_monitor = threading.Event()
        monitor = None
        if self.progress or self.verbose:
            monitor = threading.Thread(target=self._monitor, args=(stop_monitor,), daemon=True)
            monitor.start()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        stop_monitor.set()
        if monitor:
            monitor.join(timeout=2)
        return self.stats

    def add(self, task):
        with self.cond:
            self.queue.append(task)
            self.cond.notify()

    def client(self, session_key):
        with self._client_lock:
            client = self._clients.get(session_key)
            if client is not None:
                cache_hit("s3-client", session_key)
            if client is None:
                if self.client_factory:
                    client = self.client_factory(session_key)
                else:
                    config = Config(
                        retries={"mode": "adaptive", "max_attempts": 10},
                        max_pool_connections=max(10, self.workers),
                    )
                    session = self.runner.session(session_key)
                    client = session.client("s3", config=config)
                    if self.fast:
                        client = FastLister(client, session,
                                            pool_size=max(10, self.workers))
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
        with self.cond:
            if len(self.samples) < 12:
                self.samples.append(task.seed())
        session_key = self.runner.session_key(task.profile, task.region)
        client = self.client(session_key)
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
                    if not self._emit_object(item, task, client, session_key):
                        return
                    progress = key
                if delimiter:
                    for cp in response.get("CommonPrefixes") or []:
                        child = cp["Prefix"]
                        self._inc("prefixes")
                        progress = max(progress, sentinel_after(child))
                        if self.recurse:
                            self.add(
                                replace(task, prefix=child, delimiters=rest,
                                        start_after="", end_at="")
                            )
                            fan += 1
                        if self.emit_prefixes or not self.recurse:
                            if not self._emit_prefix(child, task, delimiter, session_key):
                                return
                if not response.get("IsTruncated"):
                    return
                token = response.get("NextContinuationToken")
                if delimiter and self.recurse and (
                    fan > self.max_fan
                    or (fan == 0 and pages >= self.split_after)
                ):
                    # over-shattered (fan too big) or under-shattered (the
                    # delimiter never appears, so this is a de-facto leaf
                    # paginating serially): everything <= progress is handled;
                    # hand the rest of the keyspace to the splitter path
                    self._inc("abandons")
                    if self.verbose:
                        print(
                            f"ajl: scan fan {fan} after {pages} pages under "
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

    def _count_emit(self, stat):
        with self.cond:
            if self.max_items and self.emitted >= self.max_items:
                self.stopped = True
                self.cond.notify_all()
                return False
            self.emitted += 1
            self.stats[stat] += 1
            return True

    def _emit_object(self, item, task, client, session_key):
        if not self._count_emit("objects"):
            return False
        key = item["Key"]
        record = {"Type": "s3:object", "Uri": f"s3://{task.bucket}/{key}"}
        if self.include_tags:
            record["Tags"] = self._object_tags(client, task.bucket, key)
        record["Bucket"] = task.bucket
        record.update(item)
        self.emitter.emit(record, session_key)
        return True

    def _object_tags(self, client, bucket, key):
        self._inc("calls")
        try:
            response = client.get_object_tagging(Bucket=bucket, Key=key)
        except Exception as exc:
            self._inc("tag_errors")
            if self.verbose:
                print(f"ajl: get-object-tagging failed for s3://{bucket}/{key}: {exc}",
                      file=sys.stderr)
            return {}
        return tags_to_map(response.get("TagSet"))

    def _emit_prefix(self, prefix, task, delimiter, session_key):
        if not self._count_emit("emitted_prefixes"):
            return False
        self.emitter.emit(
            {
                "Type": "s3:prefix",
                "Uri": f"s3://{task.bucket}/{prefix}",
                "Bucket": task.bucket,
                "Prefix": prefix,
                "Delimiter": delimiter,
            },
            session_key,
        )
        return True

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
        """Live progress: a tqdm counter on a TTY, plain stderr lines under
        --verbose (log-friendly)."""
        bar = None
        if self.progress:
            from tqdm import tqdm

            bar = tqdm(desc=f"ajl s3 {self.name}", unit=" obj", file=sys.stderr,
                       dynamic_ncols=True)
        # a total-less tqdm raises TypeError on bool(), so compare to None
        interval = 0.5 if bar is not None else 5
        while not stop.wait(interval):
            with self.cond:
                stats = dict(self.stats)
                queued = len(self.queue)
            if bar is not None:
                bar.n = stats["objects"]
                bar.set_postfix(
                    tasks=stats["tasks"], queued=queued, prefixes=stats["prefixes"],
                    splits=stats["splits"], failures=stats["failures"], refresh=False,
                )
                bar.refresh()
            if self.verbose:
                line = " ".join(f"{k}={v}" for k, v in stats.items())
                print(f"ajl: {self.name} progress {line} queued={queued}", file=sys.stderr)
        if bar is not None:
            bar.n = self.stats["objects"]
            bar.refresh()
            bar.close()


def parse_uri(uri):
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// uri: {uri!r}")
    bucket, _, prefix = uri[5:].partition("/")
    if not bucket:
        raise ValueError(f"missing bucket in uri: {uri!r}")
    return bucket, prefix


def seed_task(line, default_delimiters):
    """Build a Task from a --params-json line (accepts records emitted by
    ajl itself: Bucket/Prefix or Uri, a s3:prefix record's Delimiter, plus
    optional StartAfter/EndAt and profile/region in either case)."""
    bucket = line.get("Bucket") or line.get("bucket")
    prefix = line.get("Prefix") or line.get("prefix") or ""
    uri = line.get("Uri") or line.get("uri")
    if not bucket and uri:
        bucket, prefix = parse_uri(uri)
    if not bucket:
        raise ValueError("line has no 'Bucket' or 'Uri'")
    end_at = line.get("EndAt") or ""
    delimiters = tuple(line.get("Delimiters") or default_delimiters)
    if not delimiters and line.get("Delimiter"):
        delimiters = (line["Delimiter"],)  # piped s3:prefix records repeat their level
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
    parser.add_argument("--uri", action="append", default=[], dest="uri_flags",
                        metavar="s3://bucket/prefix",
                        help="same as the positional uris")
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
    parser.add_argument("--include-tags", action="store_true", default=False,
                        help="add a Tags map via one get-object-tagging call per "
                        "object (expensive at volume)")
    parser.add_argument("--no-progress", action="store_true", default=False,
                        help="disable the live progress line (auto-disabled when "
                        "stderr is not a terminal)")
    parser.add_argument("--no-fast", action="store_true", default=False,
                        help="list via boto3 instead of the raw signed HTTP fast "
                        "path (slower; escape hatch for exotic setups)")
    parser.add_argument("--failed-out", default=None, metavar="FILE",
                        help="write failed tasks as JSONL seeds for re-running "
                        "via --params-json")
    return parser


def build_list_parser():
    parser = argparse.ArgumentParser(
        prog="ajl s3 list",
        description="Simplified list-objects-v2: uri-addressed lean records; "
        "s3:prefix records pipe straight back into the next `ajl s3 list "
        "--params-json -` (each line repeats its own Delimiter until a --jq "
        "'del(.Delimiter)' makes the final stage recursive).",
        epilog="The global ajl flags also apply: --workers (pool size), "
        "--profile/--region, --max-items, --jq, --stamp-session, "
        "--params-json (seed listings from JSONL) and --verbose.",
    )
    parser.add_argument("uris", nargs="*", metavar="s3://bucket/prefix")
    parser.add_argument("--uri", action="append", default=[], dest="uri_flags",
                        metavar="s3://bucket/prefix",
                        help="same as the positional uris")
    parser.add_argument("--delimiter", default=None,
                        help="group keys into s3:prefix records at this delimiter")
    parser.add_argument("--start-after", default=None, metavar="KEY",
                        help="list keys after this one (uri seeds only)")
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE, metavar="N",
                        help="MaxKeys per request (default 1000)")
    parser.add_argument("--include-tags", action="store_true", default=False,
                        help="add a Tags map via one get-object-tagging call per "
                        "object (expensive at volume)")
    parser.add_argument("--no-progress", action="store_true", default=False,
                        help="disable the live progress line (auto-disabled when "
                        "stderr is not a terminal)")
    parser.add_argument("--no-fast", action="store_true", default=False,
                        help="list via boto3 instead of the raw signed HTTP fast "
                        "path (slower; escape hatch for exotic setups)")
    parser.add_argument("--failed-out", default=None, metavar="FILE",
                        help="write failed listings as JSONL seeds")
    return parser


def collect_seeds(options, parsed, delimiters, start_after=""):
    seeds = []
    for uri in [*parsed.uris, *parsed.uri_flags]:
        bucket, prefix = parse_uri(uri)
        seeds.append(Task(bucket=bucket, prefix=prefix, delimiters=delimiters,
                          start_after=start_after))
    if options.params_json:
        source = sys.stdin if options.params_json == "-" else open(options.params_json)
        with jsonlines.Reader(source) as reader:
            for line in reader:
                try:
                    if not isinstance(line, dict):
                        raise ValueError("not a JSON object")
                    seeds.append(seed_task(line, delimiters))
                except ValueError as exc:
                    print(f"ajl: skipping seed line: {exc}", file=sys.stderr)
    return seeds


def _show_progress(parsed):
    return not parsed.no_progress and sys.stderr.isatty()


def _finish(scanner, seeds, name, started, report=None):
    stats = scanner.run(seeds)
    if report is not None:
        report["Stats"] = dict(stats)
        report["Slices"] = list(scanner.samples)
    summary = " ".join(f"{k}={v}" for k, v in stats.items())
    print(f"ajl: {name} done in {time.time() - started:.1f}s {summary}", file=sys.stderr)
    return 1 if stats["failures"] else 0


def run_scan(runner, emitter, options, tokens, report=None):
    """Entry point from main(); tokens are the args after 'ajl s3 scan'."""
    scan_options = build_scan_parser().parse_args(tokens)
    delimiters = tuple(d for d in re.split(r"[,\s]+", scan_options.delimiters) if d)
    seeds = collect_seeds(options, scan_options, delimiters)
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
        include_tags=scan_options.include_tags,
        failed_out=failed_fp,
        workers=options.workers,
        verbose=options.verbose,
        progress=_show_progress(scan_options),
        fast=not scan_options.no_fast,
        name="scan",
    )
    started = time.time()
    try:
        return _finish(scanner, seeds, "scan", started, report)
    finally:
        if failed_fp:
            failed_fp.close()


def run_list(runner, emitter, options, tokens, report=None):
    """Entry point from main(); tokens are the args after 'ajl s3 list'."""
    list_options = build_list_parser().parse_args(tokens)
    delimiters = (list_options.delimiter,) if list_options.delimiter else ()
    seeds = collect_seeds(options, list_options, delimiters,
                          start_after=list_options.start_after or "")
    if not seeds:
        print("ajl: s3 list needs s3://bucket[/prefix] uris or --params-json",
              file=sys.stderr)
        return 2

    failed_fp = open(list_options.failed_out, "w") if list_options.failed_out else None
    scanner = Scanner(
        runner,
        emitter,
        splitter=None,
        recurse=False,
        page_size=list_options.page_size,
        max_items=options.max_items,
        include_tags=list_options.include_tags,
        failed_out=failed_fp,
        workers=options.workers,
        verbose=options.verbose,
        progress=_show_progress(list_options),
        fast=not list_options.no_fast,
        name="list",
    )
    started = time.time()
    try:
        return _finish(scanner, seeds, "list", started, report)
    finally:
        if failed_fp:
            failed_fp.close()
