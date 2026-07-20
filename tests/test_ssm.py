import io
import json
from types import SimpleNamespace

import pytest

from ajl.main import Emitter, Runner
from ajl import seal, ssm


@pytest.fixture(autouse=True)
def _passphrase(monkeypatch):
    # a passphrase is the simplest sealing config for tests
    for var in ("AJL_AGE_IDENTITY", "AJL_AGE_RECIPIENTS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AJL_AGE_PASSPHRASE", "test-pw")


class FakeSSM:
    def __init__(self, params):
        # params: {name: {"Value":..,"Type":..,"Version":..,"ARN":..}}
        self.params = params
        self.calls = []

    def get_parameter(self, Name, WithDecryption=True):
        self.calls.append(("get_parameter", Name, WithDecryption))
        if Name not in self.params:
            raise RuntimeError("ParameterNotFound")
        return {"Parameter": {"Name": Name, **self.params[Name]}}

    def get_parameters(self, Names, WithDecryption=True):
        self.calls.append(("get_parameters", tuple(Names), WithDecryption))
        assert len(Names) <= 10, "GetParameters max is 10"
        found, invalid = [], []
        for name in Names:
            if name in self.params:
                found.append({"Name": name, **self.params[name]})
            else:
                invalid.append(name)
        return {"Parameters": found, "InvalidParameters": invalid}

    def describe_parameters(self, ParameterFilters=None, MaxResults=10):
        name = ParameterFilters[0]["Values"][0]
        self.calls.append(("describe", name))
        if name not in self.params:
            return {"Parameters": []}
        p = self.params[name]
        return {"Parameters": [{"Name": name, "Type": p["Type"],
                                "KeyId": p.get("KeyId"), "Tier": p.get("Tier"),
                                "Version": p.get("Version")}]}

    def put_parameter(self, Name, Value, Type, Overwrite=False, KeyId=None,
                      Tier=None, Description=None):
        self.calls.append(("put", Name, Value, Type, Overwrite, KeyId))
        if Name in self.params and not Overwrite:
            raise RuntimeError("ParameterAlreadyExists")
        version = self.params.get(Name, {}).get("Version", 0) + 1
        self.params[Name] = {"Value": Value, "Type": Type, "KeyId": KeyId,
                             "Tier": Tier, "Version": version}
        return {"Version": version, "Tier": Tier or "Standard"}

    def get_paginator(self, op):
        assert op == "get_parameters_by_path"
        return self

    def paginate(self, Path, Recursive, WithDecryption):
        self.calls.append(("path", Path, Recursive, WithDecryption))
        matched = [
            {"Name": n, **v} for n, v in self.params.items()
            if n.startswith(Path) and (Recursive or "/" not in n[len(Path):].strip("/"))
        ]
        yield {"Parameters": matched}


def run_get(params_store, tokens, workers=4):
    runner = Runner(default_region="us-east-1")
    client = FakeSSM(params_store)
    runner._clients[(runner.session_key(), "ssm", True)] = client
    out = io.StringIO()
    options = SimpleNamespace(workers=workers)
    code = ssm.run_get(runner, Emitter(stream=out), options, tokens)
    lines = out.getvalue().splitlines()
    records = []  # raw lines pass through unparsed (--name/--raw emit bare values)
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            records.append(line)
    return code, records, client


PARAMS = {
    "/app/db/host": {"Value": "db.example.com", "Type": "String", "Version": 3,
                     "ARN": "arn:aws:ssm:us-east-1:1:parameter/app/db/host"},
    "/app/db/password": {"Value": "s3cr3t", "Type": "SecureString", "Version": 7,
                         "ARN": "arn:aws:ssm:us-east-1:1:parameter/app/db/password"},
}


def test_single_name_returns_raw_value():
    code, records, client = run_get(PARAMS, ["--name", "/app/db/password"])
    assert code == 0
    assert records == ["s3cr3t"]  # single --name -> the bare value, not a record
    assert client.calls[0] == ("get_parameter", "/app/db/password", True)


def test_single_name_json_returns_record():
    code, records, _ = run_get(PARAMS, ["--name", "/app/db/password", "--json"])
    (record,) = records
    assert record["ajl"]["type"] == "ssm:parameter" and record["Value"] == "s3cr3t"


def test_single_name_encrypt_seals_raw_value():
    code, records, _ = run_get(PARAMS, ["--name", "/app/db/password", "--encrypt"])
    (sealed,) = records  # raw sealed string
    assert seal.is_sealed(sealed)
    assert seal.unseal_value(sealed) == "s3cr3t"


def test_no_decryption_flag_not_sent_as_param():
    code, records, client = run_get(PARAMS, ["--name", "/app/db/host", "--no-decryption"])
    assert client.calls[0] == ("get_parameter", "/app/db/host", False)  # WithDecryption=False


def test_bulk_names_seals_securestring_only():
    code, records, client = run_get(
        PARAMS, ["--names", "/app/db/host", "/app/db/password"])
    assert code == 0
    by_name = {r["Name"]: r for r in records}
    assert by_name["/app/db/host"]["Value"] == "db.example.com"       # String: plaintext
    assert seal.is_sealed(by_name["/app/db/password"]["Value"])       # SecureString: sealed
    assert seal.unseal_value(by_name["/app/db/password"]["Value"]) == "s3cr3t"


def test_bulk_decrypt_forces_plaintext():
    code, records, _ = run_get(
        PARAMS, ["--names", "/app/db/password", "--decrypt"])
    (record,) = records
    assert record["Value"] == "s3cr3t"


def test_bulk_chunks_at_ten():
    store = {f"/n/{i}": {"Value": str(i), "Type": "String"} for i in range(25)}
    code, records, client = run_get(store, ["--names", *store.keys()], workers=1)
    assert code == 0
    assert len(records) == 25
    get_calls = [c for c in client.calls if c[0] == "get_parameters"]
    assert len(get_calls) == 3  # 10 + 10 + 5
    assert all(len(c[1]) <= 10 for c in get_calls)


def test_invalid_parameters_warned_and_exit_1():
    code, records, _ = run_get(PARAMS, ["--names", "/app/db/host", "/nope"])
    assert code == 1  # an invalid name -> non-zero
    assert [r["Name"] for r in records] == ["/app/db/host"]


def test_path_recursive():
    code, records, client = run_get(PARAMS, ["--path", "/app", "--recursive"])
    assert code == 0
    assert {r["Name"] for r in records} == set(PARAMS)
    assert client.calls[0] == ("path", "/app", True, True)


def test_bulk_seal_requires_key(monkeypatch):
    monkeypatch.delenv("AJL_AGE_PASSPHRASE", raising=False)
    code, records, _ = run_get(PARAMS, ["--names", "/app/db/password"])
    assert code == 2  # no key + sealing default -> refuse
    assert records == []


def test_needs_exactly_one_selector():
    code, _, _ = run_get(PARAMS, [])
    assert code == 2
    code, _, _ = run_get(PARAMS, ["--name", "/x", "--path", "/y"])
    assert code == 2


def test_decrypt_filter_round_trip(monkeypatch):
    sealed = seal.seal_value("topsecret")
    line = json.dumps({"Name": "/x", "Value": sealed, "Other": "plain"})
    monkeypatch.setattr("sys.stdin", io.StringIO(line + "\n"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    ssm.run_decrypt_filter(SimpleNamespace())
    (record,) = [json.loads(line) for line in out.getvalue().splitlines()]
    assert record["Value"] == "topsecret"
    assert record["Other"] == "plain"


def test_seal_idempotent_and_passthrough():
    once = seal.seal_value("x")
    assert seal.seal_value(once) == once          # sealing a sealed value is a no-op
    assert seal.unseal_value("not-sealed") == "not-sealed"
    assert seal.unseal_obj({"a": [seal.seal_value("y"), "z"]}) == {"a": ["y", "z"]}


def run_write(params_store, tokens, mode, workers=1, params_json=None):
    runner = Runner(default_region="us-east-1")
    client = FakeSSM(params_store)
    runner._clients[(runner.session_key(), "ssm", True)] = client
    out = io.StringIO()
    options = SimpleNamespace(workers=workers, params_json=params_json)
    code = ssm.run_write(runner, Emitter(stream=out), options, tokens, mode)
    records = [json.loads(line) for line in out.getvalue().splitlines()]
    return code, records, client


def test_update_preserves_type_and_key():
    store = {"/db/pw": {"Value": "old", "Type": "SecureString", "KeyId": "key-1",
                        "Tier": "Standard", "Version": 4}}
    code, records, client = run_write(store, ["--name", "/db/pw", "--value", "new"], "update")
    assert code == 0
    (record,) = records
    assert record["Action"] == "updated"
    put = [c for c in client.calls if c[0] == "put"][0]
    # ("put", Name, Value, Type, Overwrite, KeyId) — type & key preserved from describe
    assert put[3] == "SecureString" and put[4] is True and put[5] == "key-1"
    assert store["/db/pw"]["Value"] == "new"


def test_update_skips_unchanged():
    store = {"/x": {"Value": "same", "Type": "String", "Version": 1}}
    code, records, client = run_write(store, ["--name", "/x", "--value", "same"], "update")
    assert code == 0
    assert records[0]["Action"] == "unchanged"
    assert not [c for c in client.calls if c[0] == "put"]  # no write


def test_update_force_writes_unchanged():
    store = {"/x": {"Value": "same", "Type": "String", "Version": 1}}
    code, records, client = run_write(
        store, ["--name", "/x", "--value", "same", "--force"], "update")
    assert records[0]["Action"] == "updated"
    assert [c for c in client.calls if c[0] == "put"]


def test_update_missing_param_errors():
    code, records, _ = run_write({}, ["--name", "/nope", "--value", "v"], "update")
    assert code == 1
    assert records == []


def test_put_creates_with_explicit_type():
    store = {}
    code, records, client = run_write(
        store, ["--name", "/new", "--value", "v", "--type", "SecureString",
                "--key-id", "alias/app"], "put")
    assert code == 0 and records[0]["Action"] == "put"
    assert store["/new"]["Type"] == "SecureString" and store["/new"]["KeyId"] == "alias/app"


def test_put_no_overwrite_fails_on_existing():
    store = {"/x": {"Value": "a", "Type": "String", "Version": 1}}
    code, records, _ = run_write(store, ["--name", "/x", "--value", "b"], "put")
    assert code == 1  # exists, no --overwrite


def test_put_overwrite_flag():
    store = {"/x": {"Value": "a", "Type": "String", "Version": 1}}
    code, records, _ = run_write(
        store, ["--name", "/x", "--value", "b", "--overwrite"], "put")
    assert code == 0 and store["/x"]["Value"] == "b"


def test_write_unseals_sealed_value():
    store = {"/db/pw": {"Value": "old", "Type": "SecureString", "KeyId": "k", "Version": 1}}
    sealed = seal.seal_value("brand-new-secret")
    code, records, client = run_write(store, ["--name", "/db/pw", "--value", sealed], "update")
    assert code == 0
    assert store["/db/pw"]["Value"] == "brand-new-secret"  # unsealed before write


def test_write_streaming_params_json(monkeypatch):
    store = {}
    lines = "\n".join(json.dumps({"Name": f"/n/{i}", "Value": str(i)}) for i in range(3))
    monkeypatch.setattr("sys.stdin", io.StringIO(lines + "\n"))
    code, records, client = run_write(store, [], "put", params_json="-")
    assert code == 0 and len(records) == 3
    assert set(store) == {"/n/0", "/n/1", "/n/2"}


def test_raw_value_text():
    runner = Runner(default_region="us-east-1")
    runner._clients[(runner.session_key(), "ssm", True)] = FakeSSM(PARAMS)
    out = io.StringIO()
    ssm.run_get(runner, Emitter(stream=out), SimpleNamespace(workers=1),
                ["--name", "/app/db/host", "--raw"])
    assert out.getvalue() == "db.example.com\n"


def test_raw_bulk_values_one_per_line():
    runner = Runner(default_region="us-east-1")
    runner._clients[(runner.session_key(), "ssm", True)] = FakeSSM(PARAMS)
    out = io.StringIO()
    ssm.run_get(runner, Emitter(stream=out), SimpleNamespace(workers=1),
                ["--names", "/app/db/host", "/app/db/password", "--raw"])
    assert set(out.getvalue().splitlines()) == {"db.example.com", "s3cr3t"}
