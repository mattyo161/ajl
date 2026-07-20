import io
import json

from ajl.main import Emitter
from ajl.tags import TagMergeEmitter, fetch_tags_for_arns


class FakeTaggingClient:
    def __init__(self):
        self.calls = []

    def get_resources(self, **params):
        arns = params["ResourceARNList"]
        self.calls.append(arns)
        return {
            "ResourceTagMappingList": [
                {"ResourceARN": arn, "Tags": [{"Key": "Name", "Value": f"name-of-{arn[-1]}"}]}
                for arn in arns
                if arn.endswith(("1", "2"))
            ]
        }


def test_fetch_tags_for_arns():
    client = FakeTaggingClient()
    tags = fetch_tags_for_arns(client, ["arn:a:1", "arn:a:9"])
    assert tags == {"arn:a:1": {"Name": "name-of-1"}}


def make_emitter(batch_size=2):
    out = io.StringIO()
    client = FakeTaggingClient()
    emitter = TagMergeEmitter(
        Emitter(stream=out), lambda key: client, workers=2, batch_size=batch_size
    )
    return emitter, client, out


def records(out):
    return [json.loads(line) for line in out.getvalue().splitlines()]


def ajl_record(id_, arn):
    return {"ajl": {"id": id_, "arn": arn, "name": "", "tags": {}}}


def test_tag_merge_emitter_batches_and_merges():
    emitter, client, out = make_emitter(batch_size=2)
    emitter.emit(ajl_record("1", "arn:a:1"))
    emitter.emit(ajl_record("2", "arn:a:2"))
    emitter.emit(ajl_record("9", "arn:a:9"))
    emitter.flush()

    result = {record["ajl"]["id"]: record for record in records(out)}
    assert len(result) == 3
    assert result["1"]["ajl"]["tags"] == {"Name": "name-of-1"}
    assert result["1"]["ajl"]["name"] == "name-of-1"  # name backfilled from fetched tags
    assert result["9"]["ajl"]["tags"] == {}
    # batch of 2 submitted, then the flush remainder
    assert [len(call) for call in client.calls] == [2, 1]


def test_tag_merge_emitter_passthrough():
    emitter, client, out = make_emitter()
    emitter.emit({"ajl": {"id": "1", "arn": "arn:a:1", "tags": {"Env": "prod"}}})  # has tags
    emitter.emit({"ajl": {"id": "2", "arn": "", "tags": {}}})  # no arn
    emitter.emit({"NoAjlKey": True})  # no ajl metadata at all
    emitter.emit("not-a-dict")
    emitter.flush()
    assert client.calls == []
    assert len(out.getvalue().splitlines()) == 4
