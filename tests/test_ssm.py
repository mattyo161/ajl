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
    runner._clients[(runner.session_key(), "ssm")] = client
    out = io.StringIO()
    options = SimpleNamespace(workers=workers)
    code = ssm.run_get(runner, Emitter(stream=out), options, tokens)
    records = [json.loads(line) for line in out.getvalue().splitlines()]
    return code, records, client


PARAMS = {
    "/app/db/host": {"Value": "db.example.com", "Type": "String", "Version": 3,
                     "ARN": "arn:aws:ssm:us-east-1:1:parameter/app/db/host"},
    "/app/db/password": {"Value": "s3cr3t", "Type": "SecureString", "Version": 7,
                         "ARN": "arn:aws:ssm:us-east-1:1:parameter/app/db/password"},
}


def test_single_name_plaintext_by_default():
    code, records, client = run_get(PARAMS, ["--name", "/app/db/password"])
    assert code == 0
    (record,) = records
    assert record["Type"] == "ssm:parameter"
    assert record["Value"] == "s3cr3t"  # single --name -> plaintext
    assert client.calls[0] == ("get_parameter", "/app/db/password", True)


def test_single_name_encrypt_seals():
    code, records, _ = run_get(PARAMS, ["--name", "/app/db/password", "--encrypt"])
    (record,) = records
    assert seal.is_sealed(record["Value"])
    assert seal.unseal_value(record["Value"]) == "s3cr3t"


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
