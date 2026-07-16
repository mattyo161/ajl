import io
import json
import sys
import time
from types import SimpleNamespace

import pytest

from ajl.cache import ResultCache, parse_duration, run_cache_command
from ajl import learn


def make_options(tmp_path, monkeypatch, **overrides):
    monkeypatch.setenv("AJL_CACHE_DIR", str(tmp_path / "cache"))
    for var in ("AJL_CACHE", "AJL_AGE_IDENTITY", "AJL_AGE_RECIPIENTS",
                "AJL_AGE_PASSPHRASE", "AJL_CACHE_RM_AFTER"):
        monkeypatch.delenv(var, raising=False)
    defaults = dict(cache="15m", refresh=False, rm_after=None, params_json=None,
                    profile=None, region=None, no_parse=False, no_paginate=False,
                    max_items=None, fetch_tags=False, jq=None, stamp_session=False)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def write_and_commit(cache, key, lines, duration=2.5):
    writer = cache.open_writer(key, ["iam", "list-users"])
    for line in lines:
        writer.write(line + "\n")
    writer.commit(duration)
    return writer


def replay_to_string(cache, key, capsysbinary=None):
    # try_replay writes to sys.stdout.buffer; capture via monkey stdout
    return cache.try_replay(key)


def test_parse_duration():
    assert parse_duration("90") == 90
    assert parse_duration("15m") == 900
    assert parse_duration("2h") == 7200
    assert parse_duration("7d") == 604800
    with pytest.raises(ValueError):
        parse_duration("")


def test_key_stability_and_sensitivity(tmp_path, monkeypatch):
    options = make_options(tmp_path, monkeypatch)
    cache = ResultCache(options)
    key1 = cache.key(options, "iam", "list-users", [])
    key2 = ResultCache(options).key(options, "iam", "list-users", [])
    assert key1 == key2
    assert cache.key(options, "iam", "list-roles", []) != key1
    options.jq = ".Arn"
    assert ResultCache(options).key(options, "iam", "list-users", []) != key1


def test_round_trip_plaintext(tmp_path, monkeypatch, capfd):
    options = make_options(tmp_path, monkeypatch)
    cache = ResultCache(options)
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), write_through=True))
    key = cache.key(options, "iam", "list-users", [])
    write_and_commit(cache, key, ['{"Id":"a"}', '{"Id":"b"}'])

    stdout = io.TextIOWrapper(io.BytesIO(), write_through=True)
    monkeypatch.setattr(sys, "stdout", stdout)
    meta = ResultCache(options).try_replay(key)
    assert meta is not None and meta["lines"] == 2
    stdout.buffer.seek(0)
    assert stdout.buffer.read().decode() == '{"Id":"a"}\n{"Id":"b"}\n'
    assert "cache hit" in capfd.readouterr().err


def test_stale_entry_misses_and_sweep_removes(tmp_path, monkeypatch):
    options = make_options(tmp_path, monkeypatch, cache="15m", rm_after="1h")
    cache = ResultCache(options)
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), write_through=True))
    key = cache.key(options, "iam", "list-users", [])
    write_and_commit(cache, key, ['{"Id":"a"}'])

    plain, _, meta_path = cache._paths(key)
    meta = json.load(open(meta_path))
    meta["created"] = time.time() - 3600  # older than the 15m ttl
    json.dump(meta, open(meta_path, "w"))
    assert ResultCache(options).try_replay(key) is None  # stale -> miss

    meta["expires"] = time.time() - 10  # past rmAfter -> sweep deletes
    json.dump(meta, open(meta_path, "w"))
    ResultCache(options).sweep()
    import os
    assert not os.path.exists(plain) and not os.path.exists(meta_path)


def test_round_trip_age_encrypted(tmp_path, monkeypatch):
    from pyrage import x25519

    identity = x25519.Identity.generate()
    options = make_options(tmp_path, monkeypatch)
    monkeypatch.setenv("AJL_AGE_IDENTITY", str(identity))
    cache = ResultCache(options)
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), write_through=True))
    key = cache.key(options, "ssm", "describe-parameters", [])
    write_and_commit(cache, key, ['{"Name":"/prod/db/password"}'])
    plain, encrypted, _ = cache._paths(key)
    import os
    assert os.path.exists(encrypted) and not os.path.exists(plain)
    with open(encrypted, "rb") as fp:
        raw = fp.read()
    assert b"/prod/db/password" not in raw  # actually sealed

    stdout = io.TextIOWrapper(io.BytesIO(), write_through=True)
    monkeypatch.setattr(sys, "stdout", stdout)
    assert ResultCache(options).try_replay(key) is not None
    stdout.buffer.seek(0)
    assert b"/prod/db/password" in stdout.buffer.read()

    # lost session key -> miss, not an error (rerun regenerates)
    monkeypatch.setenv("AJL_AGE_IDENTITY", str(x25519.Identity.generate()))
    assert ResultCache(options).try_replay(key) is None


def test_failed_run_is_not_cached(tmp_path, monkeypatch):
    options = make_options(tmp_path, monkeypatch)
    cache = ResultCache(options)
    key = cache.key(options, "iam", "list-users", [])
    writer = cache.open_writer(key, ["iam", "list-users"])
    monkeypatch.setattr(writer, "stdout", io.StringIO())
    writer.write('{"Id":"partial"}\n')
    writer.abort()
    assert ResultCache(options).try_replay(key) is None


def test_disabled_cache_is_noop(tmp_path, monkeypatch):
    options = make_options(tmp_path, monkeypatch, cache=None)
    cache = ResultCache(options)
    assert not cache.enabled
    assert cache.try_replay("anything") is None
    assert cache.open_writer("anything", []) is None


def test_cache_ls_and_clear(tmp_path, monkeypatch, capsys):
    options = make_options(tmp_path, monkeypatch)
    cache = ResultCache(options)
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), write_through=True))
    key = cache.key(options, "iam", "list-users", [])
    write_and_commit(cache, key, ['{"Id":"a"}'])
    monkeypatch.undo()
    monkeypatch.setenv("AJL_CACHE_DIR", str(tmp_path / "cache"))

    assert run_cache_command("ls", []) == 0
    listing = capsys.readouterr().out.strip().splitlines()
    assert len(listing) == 1
    record = json.loads(listing[0])
    assert record["Lines"] == 1 and record["Encrypted"] is False

    assert run_cache_command("clear", ["--all"]) == 0
    assert run_cache_command("ls", []) == 0
    assert capsys.readouterr().out.strip() == ""


def test_cache_keygen(capsys):
    assert run_cache_command("keygen", []) == 0
    out = capsys.readouterr().out
    assert "# public key: age1" in out
    assert "AGE-SECRET-KEY-" in out


def test_learn_aws_equivalent_and_log(tmp_path, monkeypatch):
    equivalent = learn.aws_equivalent(
        "ec2", "describe-instances",
        {"MaxResults": 5, "DryRun": True, "Filters": [{"Name": "vpc-id"}]},
        profile="prod", region="us-west-2",
    )
    assert equivalent.startswith("aws ec2 describe-instances --max-results 5 --dry-run")
    assert "--filters '[{" in equivalent
    assert equivalent.endswith("--profile prod --region us-west-2")
    # s3 operations map to the s3api namespace
    assert learn.aws_equivalent("s3", "list-buckets", {}).startswith("aws s3api list-buckets")

    log_path = tmp_path / "learn.jsonl"
    monkeypatch.setenv("AJL_LEARN_FILE", str(log_path))
    learn.log({"Command": "ajl iam list-users", "ExitCode": 0, "DurationS": 1.2})
    learn.log({"Command": "ajl iam list-roles", "ExitCode": 0, "DurationS": 0.8})
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["Command"] == "ajl iam list-users"
    assert lines[0]["Ts"]


def test_debug_cache_hit_env_gated(monkeypatch, capsys):
    from ajl.debug import cache_hit

    monkeypatch.delenv("AJL_DEBUG_CACHE", raising=False)
    cache_hit("client", "x")
    assert capsys.readouterr().err == ""
    monkeypatch.setenv("AJL_DEBUG_CACHE", "1")
    cache_hit("client", ("dev", "us-east-1"))
    assert "cache hit client" in capsys.readouterr().err


def test_no_cache_overrides_env_default(tmp_path, monkeypatch):
    options = make_options(tmp_path, monkeypatch, cache=None, no_cache=True)
    monkeypatch.setenv("AJL_CACHE", "15m")
    assert not ResultCache(options).enabled
    options = make_options(tmp_path, monkeypatch, cache="15m", no_cache=True)
    assert not ResultCache(options).enabled


def test_no_learn_overrides_env_default(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("AJL_LEARN", "1")
    assert learn.enabled(SimpleNamespace(learn=False, no_learn=False))
    assert not learn.enabled(SimpleNamespace(learn=False, no_learn=True))
    assert not learn.enabled(SimpleNamespace(learn=True, no_learn=True))
