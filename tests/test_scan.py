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

    def __init__(self, keys, tags=None):
        self.keys = sorted(keys)
        self.tags = tags or {}
        self.calls = 0

    def get_object_tagging(self, Bucket, Key, **_):
        self.calls += 1
        if Key not in self.keys:
            raise RuntimeError("NoSuchKey")
        return {"TagSet": self.tags.get(Key, [])}

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
    emitted = [r["Key"] for r in records if r["Type"] == "s3:object"]
    assert sorted(emitted) == sorted(keys)


def test_flat_scan_small_pages():
    keys = [f"k{i:03d}" for i in range(25)]
    records, stats, _ = run_scan_over(keys, page_size=10, split_after=100)
    assert_exactly_once(records, keys)
    # lean records: Uri replaces Id/Name/Arn, Tags only under --include-tags
    assert records[0] == {
        "Type": "s3:object", "Uri": "s3://b/k000", "Bucket": "b",
        "Key": "k000", "Size": 1,
    }
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
    assert prefix_records[0] == {
        "Type": "s3:prefix", "Uri": "s3://b/a/", "Bucket": "b",
        "Prefix": "a/", "Delimiter": "/",
    }
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
    # branches 0/4/a/f at the shared "8a3145" stem, extrapolated one level
    # deeper (4^2 = 16 pairs) minus boundaries <= the already-emitted first key
    assert ranges is not None
    assert len(ranges) == 16
    assert ranges[0]["start_after"] == keys[0]
    assert ranges[-1]["end_at"] == ""
    for previous, current in zip(ranges, ranges[1:]):
        assert previous["end_at"] == current["start_after"]  # gapless partition
    assert ranges[0]["end_at"] == "8a314504"
    assert ranges[-1]["start_after"] == "8a3145ff"


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


def test_include_tags_fetches_object_tags():
    keys = ["a", "b"]
    client = FakeS3(keys, tags={"a": [{"Key": "env", "Value": "prod"}]})
    records, stats, _ = run_scan_over(keys, client=client, include_tags=True)
    by_key = {r["Key"]: r for r in records}
    assert by_key["a"]["Tags"] == {"env": "prod"}
    assert by_key["b"]["Tags"] == {}
    assert list(by_key["a"])[:3] == ["Type", "Uri", "Tags"]


def test_include_tags_errors_are_best_effort():
    class TagFailS3(FakeS3):
        def get_object_tagging(self, Bucket, Key, **_):
            raise RuntimeError("AccessDenied")

    keys = ["a"]
    records, stats, _ = run_scan_over(keys, client=TagFailS3(keys), include_tags=True)
    assert records[0]["Tags"] == {}
    assert stats["tag_errors"] == 1
    assert stats["failures"] == 0


def test_list_mode_single_level_no_recursion():
    keys = ["a/1", "a/2", "b/x/1", "top.txt"]
    tasks = [Task(bucket="b", delimiters=("/",))]
    records, stats, _ = run_scan_over(
        keys, tasks=tasks, splitter=None, recurse=False,
    )
    objects = [r["Key"] for r in records if r["Type"] == "s3:object"]
    prefixes = [r["Prefix"] for r in records if r["Type"] == "s3:prefix"]
    assert objects == ["top.txt"]          # no descent into a/ or b/
    assert sorted(prefixes) == ["a/", "b/"]
    assert stats["tasks"] == 1


def test_list_pipe_dance_covers_all_keys_once():
    # ajl s3 list --delimiter / | ajl s3 list --params-json - \
    #   | ajl s3 list --params-json - --jq 'del(.Delimiter)' | ...
    keys = [
        "a/x/1", "a/x/2", "a/y/1", "b/z/deep/very/1", "b/top", "root",
    ]
    client = FakeS3(keys)

    def stage(seed_lines, strip_delimiter=False):
        tasks = []
        for line in seed_lines:
            if strip_delimiter:
                line = {k: v for k, v in line.items() if k != "Delimiter"}
            tasks.append(seed_task(line, ()))
        records, _, _ = run_scan_over(
            keys, tasks=tasks, client=client, splitter=None, recurse=False,
        )
        objects = [r for r in records if r["Type"] == "s3:object"]
        prefixes = [r for r in records if r["Type"] == "s3:prefix"]
        return objects, prefixes

    objects1, prefixes1 = stage([{"Bucket": "b", "Delimiter": "/"}])
    objects2, prefixes2 = stage(prefixes1)                        # repeats Delimiter
    objects3, prefixes3 = stage(prefixes2, strip_delimiter=True)  # recursive tail
    emitted = [r["Key"] for r in objects1 + objects2 + objects3]
    assert sorted(emitted) == sorted(keys)
    assert prefixes3 == []  # final stage listed recursively, no more fan-out


def test_seed_task_single_delimiter_field():
    # piped s3:prefix records repeat their level; explicit schedules win
    task = seed_task({"Bucket": "b", "Prefix": "a/", "Delimiter": "/"}, ())
    assert task.delimiters == ("/",)
    task = seed_task({"Bucket": "b", "Delimiter": "-"}, ("/", "/"))
    assert task.delimiters == ("/", "/")


def test_progress_monitor_handles_totalless_tqdm():
    # tqdm with no iterable/total raises TypeError on bool(); the monitor
    # must never truth-test the bar (regression: crashed every progress run)
    import threading

    out = io.StringIO()
    scanner = Scanner(Runner(default_region="us-east-1"), Emitter(stream=out),
                      progress=True)
    stop = threading.Event()
    stop.set()  # exercise setup and the close path without looping
    scanner._monitor(stop)


def test_progress_monitor_updates_and_closes():

    keys = [f"k{i}" for i in range(5)]
    records, _, _ = run_scan_over(keys, progress=True)
    assert_exactly_once(records, keys)


def test_parse_list_xml_matches_boto_shape():
    from ajl.scan import parse_list_xml

    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>my-bucket</Name><KeyCount>3</KeyCount><MaxKeys>1000</MaxKeys>
  <IsTruncated>true</IsTruncated>
  <NextContinuationToken>tok123</NextContinuationToken>
  <EncodingType>url</EncodingType>
  <Contents>
    <Key>nrn%3Aglobal%3Ax%3A00031378</Key>
    <LastModified>2026-07-08T00:58:32.000Z</LastModified>
    <ETag>&quot;cf5ef4439c681795881e50298eb14099&quot;</ETag>
    <Size>4081</Size><StorageClass>STANDARD</StorageClass>
  </Contents>
  <Contents>
    <Key>plain/key.txt</Key>
    <LastModified>2026-07-13T16:23:24.000Z</LastModified>
    <ETag>&quot;fb827215268768253&quot;</ETag>
    <Size>158</Size><StorageClass>STANDARD</StorageClass>
  </Contents>
  <Contents><Key>sparse</Key><Size>0</Size></Contents>
  <CommonPrefixes><Prefix>a%2Fb%2F</Prefix></CommonPrefixes>
  <CommonPrefixes><Prefix>c/</Prefix></CommonPrefixes>
</ListBucketResult>"""
    response = parse_list_xml(body)
    assert response["IsTruncated"] is True
    assert response["NextContinuationToken"] == "tok123"
    assert response["KeyCount"] == 3
    assert response["Contents"][0] == {
        "Key": "nrn:global:x:00031378",
        "LastModified": "2026-07-08T00:58:32.000Z",
        "ETag": '"cf5ef4439c681795881e50298eb14099"',
        "Size": 4081,
        "StorageClass": "STANDARD",
    }
    assert response["Contents"][2] == {"Key": "sparse", "Size": 0}
    assert [p["Prefix"] for p in response["CommonPrefixes"]] == ["a/b/", "c/"]


def test_parse_list_xml_final_page():
    from ajl.scan import parse_list_xml

    body = b"""<?xml version="1.0"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>b</Name><KeyCount>0</KeyCount><IsTruncated>false</IsTruncated>
</ListBucketResult>"""
    response = parse_list_xml(body)
    assert response["IsTruncated"] is False
    assert response["Contents"] == []
    assert response["CommonPrefixes"] == []
    assert "NextContinuationToken" not in response
