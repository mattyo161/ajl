import json

import boto3
from botocore.model import OperationModel, ServiceModel

from ajl import apilog


class FakeOptions:
    def __init__(self, api_log=False, no_api_log=False):
        self.api_log = api_log
        self.no_api_log = no_api_log


def test_enabled_respects_flag_and_env(monkeypatch):
    monkeypatch.delenv("AJL_APILOG", raising=False)
    assert apilog.enabled(FakeOptions()) is False
    assert apilog.enabled(FakeOptions(api_log=True)) is True
    assert apilog.enabled(FakeOptions(api_log=True, no_api_log=True)) is False
    monkeypatch.setenv("AJL_APILOG", "1")
    assert apilog.enabled(FakeOptions()) is True


def test_log_file_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("AJL_APILOG_FILE", raising=False)
    assert apilog.log_file().endswith("ajl/apilog.jsonl")
    monkeypatch.setenv("AJL_APILOG_FILE", "/tmp/custom-apilog.jsonl")
    assert apilog.log_file() == "/tmp/custom-apilog.jsonl"


def test_item_count_sums_list_fields():
    assert apilog._item_count({"Parameters": [1, 2, 3], "ResponseMetadata": {}}) == 3
    assert apilog._item_count({"ResponseMetadata": {}}) is None
    assert apilog._item_count("not-a-dict") is None


def _fake_client():
    session = boto3.Session(
        aws_access_key_id="fake", aws_secret_access_key="fake", region_name="us-east-1"
    )
    return session.client("sts")


def test_attach_logs_success_and_error(monkeypatch, tmp_path):
    log_path = tmp_path / "apilog.jsonl"
    monkeypatch.setenv("AJL_APILOG_FILE", str(log_path))

    client = _fake_client()
    apilog.attach(client, "sts", "myprofile", "us-east-1")

    service_model = client.meta.service_model
    operation_model = OperationModel(
        {"name": "GetCallerIdentity"}, ServiceModel(service_model._service_description)
    )
    context = {}
    events = client.meta.events
    events.emit(
        "before-call.sts.GetCallerIdentity",
        model=operation_model,
        params={},
        request_signer=None,
        context=context,
    )
    events.emit(
        "after-call.sts.GetCallerIdentity",
        http_response=type("R", (), {"status_code": 200})(),
        parsed={"Account": "123", "ResponseMetadata": {"HTTPStatusCode": 200, "RetryAttempts": 2}},
        model=operation_model,
        context=context,
    )

    error_context = {"retries": {"attempt": 3}}
    exc = Exception("boom")
    exc.response = {"Error": {"Code": "Throttling"}}
    events.emit(
        "after-call-error.sts.GetCallerIdentity",
        exception=exc,
        context=error_context,
    )

    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(lines) == 2

    success = lines[0]
    assert success["Service"] == "sts"
    assert success["Operation"] == "GetCallerIdentity"
    assert success["Profile"] == "myprofile"
    assert success["Region"] == "us-east-1"
    assert success["HttpStatus"] == 200
    assert success["Attempts"] == 3
    assert success["Items"] is None
    assert success["Outcome"] == "success"

    error = lines[1]
    assert error["Operation"] == "GetCallerIdentity"
    assert error["Attempts"] == 3
    assert error["Outcome"] == "error"
    assert error["ErrorCode"] == "Throttling"
