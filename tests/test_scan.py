import io
import json

import pytest

from ajl.main import Emitter, Runner
from ajl.scan import (
    FixedRangeSplitter,
    RadixSplitter,
    Scanner,
    Task,
    builtin_splitters,
    parse_uri,
    seed_task,
    sentinel_after,
)

MAX_CHAR = "\U0010ffff"


class FakeMeta:
    partition = "aws"
    region_name = "us-east-1"


class FakeS3:
    """In-memory list-objects-v2 with prefix/delimiter/StartAfter/pagination
    semantics matching S3 (lexicographic order, CommonPrefixes collapse,
    opaque continuation tokens)."""

    meta = FakeMeta()

    def __init__(self, keys):
        self.keys = sorted(keys)
        self.calls = 0

    def list_objects_v2(self, Bucket, Prefix="", Delimiter=None, StartAfter="",
                        MaxKeys=1000, ContinuationToken=None, **_):
        self.calls += 1
        start = ContinuationToken or StartAfter or ""
        keys = [k for k in self.keys if k.startswith(Prefix) and k > start]
        contents, prefixes = [], []
        count = 0
        marker = None
        truncated = False
        i = 0
        while i < len(keys):
            if count >= MaxKeys:
                truncated = True
                break
            key = keys[i]
            if Delimiter:
                rest = key[len(Prefix):]
                pos = rest.find(Delimiter)
                if pos >= 0:
                    group = Prefix + rest[: pos + len(Delimiter)]
                    prefixes.append({"Prefix": group})
                    count += 1
                    marker = group + MAX_CHAR
                    while i < len(keys) and keys[i].startswith(group):
                        i += 1
                    continue
            contents.append({"Key": key, "Size": 1})
            count += 1
            marker = key
            i += 1
        response = {
            "Name": Bucket,
            "KeyCount": count,
            "Contents": contents,
            "CommonPrefixes": prefixes,
            "IsTruncated": truncated,
        }
        if truncated:
            response["NextContinuationToken"] = marker
        return response


class FailingS3(FakeS3):
    def __init__(self, keys, fail_prefix):
        super().__init__(keys)
        self.fail_prefix = fail_prefix

    def list_objects_v2(self, Bucket, Prefix="", **kwargs):
        if Prefix.startswith(self.fail_prefix):
            raise RuntimeError("boom")
        return super().list_objects_v2(Bucket, Prefix=Prefix, **kwargs)


def run_scan_over(keys, tasks=None, client=None, workers=4, **scanner_kwargs):
    client = client or FakeS3(keys)
    out = io.StringIO()
    emitter = Emitter(stream=out)
    runner = Runner(default_region="us-east-1")
    scanner_kwargs.setdefault("splitter", RadixSplitter())
    scanner = Scanner(
        runner, emitter, client_factory=lambda session_key: client,
        workers=workers, **scanner_kwargs,
    )
    stats = scanner.run(tasks or [Task(bucket="b")])
    records = [json.loads(line) for line in out.getvalue().splitlines()]
    return records, stats, client


def assert_exactly_once(records, keys):
    emitted = [r["Id"] for r in records if r["Type"] == "s3:object"]
    assert sorted(emitted) == sorted(keys)


def test_flat_scan_small_pages():
    keys = [f"k{i:03d}" for i in range(25)]
    records, stats, _ = run_scan_over(keys, page_size=10, split_after=100)
    assert_exactly_once(records, keys)
    assert records[0]["Type"] == "s3:object"
    assert records[0]["Arn"] == "arn:aws:s3:::b/k000"
    assert records[0]["Uri"] == "s3://b/k000"
    assert records[0]["Tags"] == {}
    assert stats["objects"] == 25


def test_delimiter_schedule_fans_out():
    keys = ["a/1.txt", "a/2.txt", "b/x/1.txt", "b/x/2.txt", "top.txt"]
    tasks = [Task(bucket="b", delimiters=("/", "/"))]
    records, stats, _ = run_scan_over(keys, tasks=tasks, page_size=2)
    assert_exactly_once(records, keys)
    assert stats["prefixes"] >= 3  # a/, b/, b/x/


def test_emit_prefixes_records():
    keys = ["a/1", "b/2"]
    tasks = [Task(bucket="b", delimiters=("/",))]
    records, _, _ = run_scan_over(keys, tasks=tasks, emit_prefixes=True)
    prefix_records = [r for r in records if r["Type"] == "s3:prefix"]
    assert {r["Prefix"] for r in prefix_records} == {"a/", "b/"}
    assert prefix_records[0]["Uri"].startswith("s3://b/")
    assert_exactly_once(records, keys)


def test_radix_split_skewed_keyspace():
    # 9.5-of-10-million-students-in-3-hashes, in miniature: most keys share a
    # long hash prefix, so fixed depth fails and radix must descend it
    keys = [f"zz{i:03d}" for i in range(40)]
    keys += [f"8a3145deadbeef{i:05d}" for i in range(800)]
    keys += [f"8a3145deadbff{i:04d}" for i in range(100)]
    records, stats, _ = run_scan_over(keys, page_size=25, split_after=2)
    assert_exactly_once(records, keys)
    assert stats["splits"] >= 1


def test_radix_split_no_dup_at_boundaries():
    # keys equal to branch boundaries and keys equal to a prefix itself
    keys = ["p", "p0", "p00", "p01", "pa", "pa0", "pz"] + [f"p0{i:02d}" for i in range(50)]
    records, _, _ = run_scan_over(keys, page_size=5, split_after=1)
    assert_exactly_once(records, keys)


def test_fixed_hex_splitter():
    keys = [f"{a}{b}{i}" for a in "0369cf" for b in "28ae" for i in range(20)]
    records, stats, _ = run_scan_over(
        keys, page_size=10, split_after=1,
        splitter=builtin_splitters()["hex2"](),
    )
    assert_exactly_once(records, keys)
    assert stats["splits"] == 1


def test_fixed_splitter_exhausted_falls_back_to_serial():
    # all keys inside one hex2 cell: the splitter can't help, the task must
    # mark itself no_split and page to completion instead of looping
    keys = [f"aa{i:04d}" for i in range(60)]
    records, stats, _ = run_scan_over(
        keys, page_size=10, split_after=1,
        splitter=FixedRangeSplitter("hex2", "0123456789abcdef", 2),
    )
    assert_exactly_once(records, keys)


def test_over_shatter_abandons_delimiter_for_ranges():
    keys = [f"p{i:03d}/k" for i in range(60)]
    tasks = [Task(bucket="b", delimiters=("/",))]
    records, stats, _ = run_scan_over(
        keys, tasks=tasks, page_size=10, max_fan=15, split_after=2,
    )
    assert_exactly_once(records, keys)
    assert stats["abandons"] >= 1


def test_max_items_stops_early():
    keys = [f"k{i:04d}" for i in range(500)]
    records, _, _ = run_scan_over(keys, page_size=50, max_items=120, split_after=100)
    assert len(records) == 120


def test_failed_task_writes_seed():
    keys = ["good/1", "good/2", "bad/1"]
    client = FailingS3(keys, fail_prefix="bad/")
    failed = io.StringIO()
    tasks = [Task(bucket="b", delimiters=("/",))]
    records, stats, _ = run_scan_over(
        keys, tasks=tasks, client=client, failed_out=failed,
    )
    assert_exactly_once(records, ["good/1", "good/2"])
    assert stats["failures"] == 1
    (seed,) = [json.loads(line) for line in failed.getvalue().splitlines()]
    assert seed == {"Bucket": "b", "Prefix": "bad/"}


def test_sentinel_after_sorts_past_prefix_keys():
    assert sentinel_after("p/") > "p/" + "z" * 1000
    assert sentinel_after("p/") < "q"
    assert len(sentinel_after("p/").encode()) <= 1100


def test_radix_branch_discovery_descends_common_prefix():
    keys = [f"8a3145{c}{i:02d}" for c in "04af" for i in range(5)]
    client = FakeS3(keys)
    from ajl.scan import SplitContext

    ctx = SplitContext(client=client, bucket="b", prefix="", last_key=keys[0],
                       end_at="", count_call=lambda: None)
    ranges = RadixSplitter().split(ctx)
    # branches 0/4/a/f minus everything <= the already-emitted first key
    assert ranges is not None
    assert [rng["start_after"] for rng in ranges][0] == keys[0]
    assert [rng["end_at"] for rng in ranges][-1] == ""
    boundaries = [rng["end_at"] for rng in ranges][:-1]
    assert boundaries == ["8a31454", "8a3145a", "8a3145f"]


def test_parse_uri():
    assert parse_uri("s3://bucket") == ("bucket", "")
    assert parse_uri("s3://bucket/a/b/") == ("bucket", "a/b/")
    with pytest.raises(ValueError):
        parse_uri("http://bucket")


def test_seed_task_accepts_ajl_records():
    task = seed_task(
        {"Type": "s3:prefix", "Bucket": "b", "Prefix": "a/", "profile": "p1"},
        default_delimiters=("/",),
    )
    assert (task.bucket, task.prefix, task.delimiters, task.profile) == ("b", "a/", ("/",), "p1")
    task = seed_task({"Uri": "s3://b/x/", "Region": "us-west-2"}, ())
    assert (task.bucket, task.prefix, task.region) == ("b", "x/", "us-west-2")
    # EndAt seeds are ranges: delimiters are dropped
    task = seed_task({"Bucket": "b", "StartAfter": "a", "EndAt": "f"}, ("/",))
    assert task.delimiters == ()
    with pytest.raises(ValueError):
        seed_task({"Prefix": "no-bucket/"}, ())


def test_multi_account_seeds_route_sessions():
    clients = {}

    def factory(session_key):
        return clients.setdefault(session_key, FakeS3(["x/1", "x/2"]))

    out = io.StringIO()
    runner = Runner(default_region="us-east-1")
    scanner = Scanner(runner, Emitter(stream=out), client_factory=factory, workers=2)
    scanner.run([
        Task(bucket="b", profile="dev"),
        Task(bucket="b", profile="prod", region="eu-west-1"),
    ])
    assert set(clients) == {("dev", "us-east-1"), ("prod", "eu-west-1")}
    assert len(out.getvalue().splitlines()) == 4
