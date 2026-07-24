import io
import json
from collections import Counter

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


class FakeVersionedS3:
    """In-memory list-object-versions: one entry (Version or DeleteMarker)
    per key, paginated via KeyMarker. Doesn't model multiple versions of
    the same key split across a page boundary (VersionIdMarker) -- ajl's
    client code threads it through symmetrically to KeyMarker regardless
    of whether this fake exercises that specific case."""

    meta = FakeMeta()

    def __init__(self, keys, delete_markers=(), tags=None):
        self.entries = sorted(keys)
        self.delete_markers = set(delete_markers)
        self.tags = tags or {}
        self.calls = 0
        self.tag_calls = 0

    def get_object_tagging(self, Bucket, Key, VersionId=None, **_):
        self.tag_calls += 1
        return {"TagSet": self.tags.get((Key, VersionId), [])}

    def list_object_versions(self, Bucket, Prefix="", Delimiter=None, KeyMarker="",
                             VersionIdMarker=None, MaxKeys=1000, **_):
        self.calls += 1
        start = KeyMarker or ""
        keys = [k for k in self.entries if k.startswith(Prefix) and k > start]
        versions, delete_markers = [], []
        count = 0
        marker = None
        truncated = False
        for key in keys:
            if count >= MaxKeys:
                truncated = True
                break
            entry = {"Key": key, "VersionId": f"v-{key}", "IsLatest": True}
            if key in self.delete_markers:
                delete_markers.append(entry)
            else:
                versions.append({**entry, "Size": 1, "ETag": '"etag"'})
            count += 1
            marker = key
        response = {
            "Name": Bucket, "Versions": versions, "DeleteMarkers": delete_markers,
            "CommonPrefixes": [], "IsTruncated": truncated,
        }
        if truncated:
            response["NextKeyMarker"] = marker
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
    emitted = [r["Key"] for r in records if r["ajl"]["type"] == "s3:object"]
    assert sorted(emitted) == sorted(keys)


def test_flat_scan_small_pages():
    keys = [f"k{i:03d}" for i in range(25)]
    records, stats, _ = run_scan_over(keys, page_size=10, split_after=100)
    assert_exactly_once(records, keys)
    # lean records: ajl.uri replaces id/name/arn, tags only under --include-tags
    assert records[0] == {
        "Bucket": "b", "Key": "k000", "Size": 1,
        "ajl": {"type": "s3:object", "uri": "s3://b/k000"},
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
    prefix_records = [r for r in records if r["ajl"]["type"] == "s3:prefix"]
    assert {r["Prefix"] for r in prefix_records} == {"a/", "b/"}
    assert prefix_records[0] == {
        "Bucket": "b", "Prefix": "a/", "Delimiter": "/",
        "ajl": {"type": "s3:prefix", "uri": "s3://b/a/"},
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
        {"Bucket": "b", "Prefix": "a/", "profile": "p1", "ajl": {"type": "s3:prefix"}},
        default_delimiters=("/",),
    )
    assert (task.bucket, task.prefix, task.delimiters, task.profile) == ("b", "a/", ("/",), "p1")
    # a raw ajl-emitted record, piped straight back in without a jq reshape:
    # bucket/prefix come from ajl.uri, region from the nested ajl.stamp
    task = seed_task({"ajl": {"uri": "s3://b/x/", "stamp": {"region": "us-west-2"}}}, ())
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
    assert by_key["a"]["ajl"]["tags"] == {"env": "prod"}
    assert by_key["b"]["ajl"]["tags"] == {}
    assert list(by_key["a"]) == ["Bucket", "Key", "Size", "ajl"]
    assert list(by_key["a"]["ajl"]) == ["type", "uri", "tags"]


def test_include_tags_errors_are_best_effort():
    class TagFailS3(FakeS3):
        def get_object_tagging(self, Bucket, Key, **_):
            raise RuntimeError("AccessDenied")

    keys = ["a"]
    records, stats, _ = run_scan_over(keys, client=TagFailS3(keys), include_tags=True)
    assert records[0]["ajl"]["tags"] == {}
    assert stats["tag_errors"] == 1
    assert stats["failures"] == 0


def test_list_mode_single_level_no_recursion():
    keys = ["a/1", "a/2", "b/x/1", "top.txt"]
    tasks = [Task(bucket="b", delimiters=("/",))]
    records, stats, _ = run_scan_over(
        keys, tasks=tasks, splitter=None, recurse=False,
    )
    objects = [r["Key"] for r in records if r["ajl"]["type"] == "s3:object"]
    prefixes = [r["Prefix"] for r in records if r["ajl"]["type"] == "s3:prefix"]
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
        objects = [r for r in records if r["ajl"]["type"] == "s3:object"]
        prefixes = [r for r in records if r["ajl"]["type"] == "s3:prefix"]
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


def test_parse_list_versions_xml_matches_boto_shape():
    from ajl.scan import parse_list_versions_xml

    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListVersionsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>my-bucket</Name>
  <IsTruncated>true</IsTruncated>
  <NextKeyMarker>nrn%3Aglobal%3Ax%3A00031378</NextKeyMarker>
  <NextVersionIdMarker>abc123</NextVersionIdMarker>
  <EncodingType>url</EncodingType>
  <Version>
    <Key>nrn%3Aglobal%3Ax%3A00031378</Key>
    <VersionId>abc123</VersionId>
    <IsLatest>true</IsLatest>
    <LastModified>2026-07-08T00:58:32.000Z</LastModified>
    <ETag>&quot;cf5ef4439c681795881e50298eb14099&quot;</ETag>
    <Size>4081</Size><StorageClass>STANDARD</StorageClass>
  </Version>
  <Version>
    <Key>nrn%3Aglobal%3Ax%3A00031378</Key>
    <VersionId>older456</VersionId>
    <IsLatest>false</IsLatest>
    <LastModified>2026-06-01T00:00:00.000Z</LastModified>
    <ETag>&quot;deadbeef&quot;</ETag>
    <Size>2000</Size><StorageClass>STANDARD</StorageClass>
  </Version>
  <DeleteMarker>
    <Key>removed.txt</Key>
    <VersionId>del789</VersionId>
    <IsLatest>true</IsLatest>
    <LastModified>2026-07-10T00:00:00.000Z</LastModified>
  </DeleteMarker>
  <CommonPrefixes><Prefix>a%2Fb%2F</Prefix></CommonPrefixes>
</ListVersionsResult>"""
    response = parse_list_versions_xml(body)
    assert response["IsTruncated"] is True
    assert response["NextKeyMarker"] == "nrn:global:x:00031378"
    assert response["NextVersionIdMarker"] == "abc123"
    assert response["Versions"][0] == {
        "Key": "nrn:global:x:00031378",
        "VersionId": "abc123",
        "IsLatest": True,
        "LastModified": "2026-07-08T00:58:32.000Z",
        "ETag": '"cf5ef4439c681795881e50298eb14099"',
        "Size": 4081,
        "StorageClass": "STANDARD",
    }
    assert response["Versions"][1]["IsLatest"] is False
    assert response["DeleteMarkers"] == [{
        "Key": "removed.txt",
        "VersionId": "del789",
        "IsLatest": True,
        "LastModified": "2026-07-10T00:00:00.000Z",
    }]
    assert [p["Prefix"] for p in response["CommonPrefixes"]] == ["a/b/"]


def test_parse_list_versions_xml_final_page():
    from ajl.scan import parse_list_versions_xml

    body = b"""<?xml version="1.0"?>
<ListVersionsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>b</Name><IsTruncated>false</IsTruncated>
</ListVersionsResult>"""
    response = parse_list_versions_xml(body)
    assert response["IsTruncated"] is False
    assert response["Versions"] == []
    assert response["DeleteMarkers"] == []
    assert response["CommonPrefixes"] == []
    assert "NextKeyMarker" not in response
    assert "NextVersionIdMarker" not in response


def test_task_seed_and_seed_task_round_trip_versions():
    task = Task(bucket="b", prefix="a/", versions=True)
    seed = task.seed()
    assert seed["Versions"] is True
    restored = seed_task(seed, default_delimiters=())
    assert restored.versions is True

    # existing behavior unaffected: no Versions key at all when unset
    plain = Task(bucket="b").seed()
    assert "Versions" not in plain
    assert seed_task(plain, ()).versions is False


def test_versions_mode_emits_object_versions_and_delete_markers():
    keys = ["a", "b", "c"]
    client = FakeVersionedS3(keys, delete_markers=["b"])
    records, stats, _ = run_scan_over(
        keys, tasks=[Task(bucket="bkt", versions=True)], client=client,
        splitter=None, recurse=False,
    )
    by_key = {r["Key"]: r for r in records}
    assert by_key["a"]["ajl"]["type"] == "s3:object-version"
    assert by_key["a"]["VersionId"] == "v-a"
    assert by_key["a"]["ETag"] == '"etag"'
    assert by_key["b"]["ajl"]["type"] == "s3:delete-marker"
    assert "ETag" not in by_key["b"]
    assert by_key["c"]["ajl"]["type"] == "s3:object-version"
    assert stats["objects"] == 3
    assert sorted(r["Bucket"] for r in records) == ["bkt"] * 3


def test_versions_mode_paginates_via_key_marker():
    keys = [f"k{i}" for i in range(5)]
    client = FakeVersionedS3(keys)
    records, stats, _ = run_scan_over(
        keys, tasks=[Task(bucket="bkt", versions=True)], client=client,
        splitter=None, recurse=False, page_size=2,
    )
    assert sorted(r["Key"] for r in records) == keys
    assert client.calls == 3  # 2 + 2 + 1 keys per page


def test_versions_mode_tags_object_versions_not_delete_markers():
    keys = ["a", "b"]
    client = FakeVersionedS3(
        keys, delete_markers=["b"],
        tags={("a", "v-a"): [{"Key": "env", "Value": "prod"}]},
    )
    records, stats, _ = run_scan_over(
        keys, tasks=[Task(bucket="bkt", versions=True)], client=client,
        splitter=None, recurse=False, include_tags=True,
    )
    by_key = {r["Key"]: r for r in records}
    assert by_key["a"]["ajl"]["tags"] == {"env": "prod"}
    assert by_key["b"]["ajl"]["tags"] == {}
    assert client.tag_calls == 1  # the delete marker never gets a tagging call


def test_plain_scan_mode_is_unaffected_by_versions_field_existing():
    # a Task built the old way (no versions kwarg at all) behaves exactly
    # as before -- the new field's default doesn't change existing runs.
    keys = ["a", "b"]
    records, stats, _ = run_scan_over(keys, splitter=None, recurse=False)
    assert sorted(r["Key"] for r in records) == keys
    assert all(r["ajl"]["type"] == "s3:object" for r in records)


class FakeMultiVersionS3:
    """Like FakeVersionedS3, but a key can carry multiple versions --
    exercises the split/abandon mid-key resume path. Unlike a plain
    list-objects-v2 key (always fully resolved by a bare KeyMarker), a
    version list can be cut mid-key by a page boundary, so resuming needs
    the VersionIdMarker too."""

    meta = FakeMeta()

    def __init__(self, keys_versions):
        self.keys_versions = keys_versions  # {key: [versionId, ...]}, newest first
        self.keys = sorted(keys_versions)
        self.calls = 0

    def get_object_tagging(self, **kwargs):
        return {"TagSet": []}

    def list_objects_v2(self, Bucket, Prefix="", StartAfter="", MaxKeys=1000, **_):
        # the splitter only ever probes the plain key alphabet with this
        keys = [k for k in self.keys if k.startswith(Prefix) and k > StartAfter]
        page = keys[:MaxKeys]
        return {"Name": Bucket, "KeyCount": len(page),
                "Contents": [{"Key": k, "Size": 1} for k in page],
                "CommonPrefixes": [], "IsTruncated": len(keys) > MaxKeys}

    def list_object_versions(self, Bucket, Prefix="", KeyMarker="", VersionIdMarker=None,
                             MaxKeys=1000, **_):
        self.calls += 1
        entries = [(k, vid) for k in self.keys if k.startswith(Prefix)
                   for vid in self.keys_versions[k]]
        if KeyMarker:
            resumed, skipping = [], True
            for key, vid in entries:
                if skipping:
                    if key < KeyMarker:
                        continue
                    if key == KeyMarker:
                        if VersionIdMarker is not None and vid == VersionIdMarker:
                            skipping = False
                        continue
                    skipping = False
                resumed.append((key, vid))
            entries = resumed
        page = entries[:MaxKeys]
        versions = [
            {"Key": k, "VersionId": vid, "IsLatest": vid == self.keys_versions[k][0],
             "Size": 1, "ETag": '"e"'}
            for k, vid in page
        ]
        truncated = len(entries) > MaxKeys
        response = {"Name": Bucket, "Versions": versions, "DeleteMarkers": [],
                    "CommonPrefixes": [], "IsTruncated": truncated}
        if truncated:
            last_key, last_vid = page[-1]
            response["NextKeyMarker"] = last_key
            response["NextVersionIdMarker"] = last_vid
        return response


def test_versions_mode_split_mid_key_drops_nothing_and_dupes_nothing():
    # regression: a split used to hand the new sub-task `start_after` =
    # the last KEY touched with no version marker, which either restarted
    # the whole task from scratch (duplicates) or silently skipped
    # whatever was left of that key's version list (data loss) depending
    # on which bug was live. page_size=2/split_after=1 forces a split
    # after page 1, which lands mid-key-"a" (3 versions, only 2 fit) --
    # 8 single-version "b*" keys give the radix splitter enough branch
    # diversity in the remaining keyspace to actually split (a single
    # leftover key isn't enough for it to bother).
    keys_versions = {"a": ["a-v3", "a-v2", "a-v1"]}
    for i in range(8):
        keys_versions[f"b{i}"] = [f"b{i}-v1"]
    client = FakeMultiVersionS3(keys_versions)
    records, stats, _ = run_scan_over(
        list(keys_versions), tasks=[Task(bucket="bkt", versions=True)],
        client=client, page_size=2, split_after=1,
    )
    emitted = Counter((r["Key"], r["VersionId"]) for r in records)
    expected = {(k, vid) for k, vids in keys_versions.items() for vid in vids}
    assert set(emitted) == expected
    assert all(count == 1 for count in emitted.values())
    assert stats["splits"] >= 1  # confirms the mid-key split path actually ran


def test_versions_mode_end_at_cutoff_does_not_skip_earlier_delete_markers():
    # regression: Versions and DeleteMarkers used to be processed as two
    # separate sequential loops (all Versions, then all DeleteMarkers) --
    # not S3's true combined key order. A key past end_at hit partway
    # through the Versions loop returned immediately, so any DeleteMarkers
    # on that same page for keys that were still in-bounds never even got
    # reached, let alone emitted. This page deliberately puts the
    # out-of-bounds entry in Versions and the in-bounds one in
    # DeleteMarkers to catch exactly that ordering bug.
    class OnePageMixedClient:
        meta = FakeMeta()

        def get_object_tagging(self, **kwargs):
            return {"TagSet": []}

        def list_object_versions(self, Bucket, **_):
            return {
                "Name": Bucket,
                "Versions": [{"Key": "c", "VersionId": "c-v1", "IsLatest": True}],
                "DeleteMarkers": [{"Key": "a", "VersionId": "a-dm1", "IsLatest": True}],
                "CommonPrefixes": [],
                "IsTruncated": False,
            }

    records, stats, _ = run_scan_over(
        ["a", "c"], tasks=[Task(bucket="bkt", versions=True, end_at="b")],
        client=OnePageMixedClient(), splitter=None, recurse=False,
    )
    by_key = {r["Key"]: r for r in records}
    assert "a" in by_key
    assert by_key["a"]["ajl"]["type"] == "s3:delete-marker"
    assert "c" not in by_key  # beyond end_at, correctly excluded
