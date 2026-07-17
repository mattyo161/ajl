import io
import json
from types import SimpleNamespace

from ajl.main import Emitter, Runner
from ajl import fanout


def opts(**kw):
    base = dict(all=False, all_profiles=False, all_regions=False,
                profile=None, region="us-east-1", workers=4, verbose=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_resolve_profiles_from_env(monkeypatch):
    monkeypatch.setenv("AJL_PROFILES", "dev, prod  staging")
    assert fanout.resolve_profiles() == ["dev", "prod", "staging"]


def test_resolve_profiles_from_config(monkeypatch, tmp_path):
    monkeypatch.delenv("AJL_PROFILES", raising=False)
    config = tmp_path / "config"
    config.write_text(
        "[default]\nregion=us-east-1\n\n"
        "[profile dev]\nregion=us-east-1\n\n"
        "[profile prod]\nregion=us-west-2\n"
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    assert set(fanout.resolve_profiles()) == {"default", "dev", "prod"}


def test_plan_dedupes_profiles_by_account(monkeypatch):
    monkeypatch.setenv("AJL_PROFILES", "a b c")
    runner = Runner(default_region="us-east-1")
    # profiles a and b are the same account (111), c is a different one (222)
    accounts = {"a": "111", "b": "111", "c": "222"}
    runner.account = lambda sk: accounts[sk[0]]
    sessions = fanout.plan_sessions(runner, opts(all_profiles=True), "ssm")
    profiles = [s[0] for s in sessions]
    assert profiles == ["a", "c"]  # b dropped as a duplicate account


def test_plan_expands_regions(monkeypatch):
    monkeypatch.setenv("AJL_PROFILES", "solo")
    runner = Runner(default_region="us-east-1")
    runner.account = lambda sk: "111"
    monkeypatch.setattr(fanout, "resolve_regions",
                        lambda r, sk, svc: ["us-east-1", "us-west-2", "eu-west-1"])
    sessions = fanout.plan_sessions(runner, opts(all=True), "ssm")
    assert sorted(s[1] for s in sessions) == ["eu-west-1", "us-east-1", "us-west-2"]


def test_run_fanout_contains_failures(monkeypatch):
    monkeypatch.setenv("AJL_PROFILES", "ok denied")
    runner = Runner(default_region="us-east-1")
    runner.account = lambda sk: sk[0]  # distinct accounts
    monkeypatch.setattr(fanout, "resolve_regions", lambda r, sk, svc: ["us-east-1"])
    out = io.StringIO()
    emitter = Emitter(stream=out)
    seen = []

    def run_one(session_key):
        if session_key[0] == "denied":
            raise RuntimeError("AccessDenied")
        seen.append(session_key)
        emitter.emit({"from": session_key[0]}, session_key)

    code = fanout.run_fanout(runner, emitter, opts(all_profiles=True), run_one, "ssm")
    assert code == 1  # a failure -> non-zero
    assert [json.loads(line)["from"] for line in out.getvalue().splitlines()] == ["ok"]


def test_resolve_regions_env_override(monkeypatch):
    monkeypatch.setenv("AJL_REGIONS", "us-east-1, eu-west-1")
    assert fanout.resolve_regions(Runner(default_region="us-east-1"),
                                  ("p", "us-east-1"), "ssm") == ["us-east-1", "eu-west-1"]


def test_resolve_regions_static_no_api(monkeypatch):
    monkeypatch.delenv("AJL_REGIONS", raising=False)
    runner = Runner(default_region="us-east-1")
    regions = fanout.resolve_regions(runner, ("default", "us-east-1"), "ssm")
    # botocore's static list — no API call, includes the usual suspects
    assert "us-east-1" in regions and "eu-west-1" in regions and len(regions) > 5


def test_plan_skips_dead_credential_profiles(monkeypatch):
    monkeypatch.setenv("AJL_PROFILES", "good dead")
    monkeypatch.setenv("AJL_REGIONS", "us-east-1")
    runner = Runner(default_region="us-east-1")
    runner.account = lambda sk: "111" if sk[0] == "good" else ""  # dead: no account
    sessions = fanout.plan_sessions(runner, opts(all_profiles=True), "ssm")
    assert [s[0] for s in sessions] == ["good"]  # dead profile dropped
