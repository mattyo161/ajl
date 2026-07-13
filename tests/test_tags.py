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


def test_tag_merge_emitter_batches_and_merges():
    emitter, client, out = make_emitter(batch_size=2)
    emitter.emit({"Id": "1", "Arn": "arn:a:1", "Name": "", "Tags": {}})
    emitter.emit({"Id": "2", "Arn": "arn:a:2", "Name": "", "Tags": {}})
    emitter.emit({"Id": "9", "Arn": "arn:a:9", "Name": "", "Tags": {}})
    emitter.flush()

    result = {record["Id"]: record for record in records(out)}
    assert len(result) == 3
    assert result["1"]["Tags"] == {"Name": "name-of-1"}
    assert result["1"]["Name"] == "name-of-1"  # Name backfilled from fetched tags
    assert result["9"]["Tags"] == {}
    # batch of 2 submitted, then the flush remainder
    assert [len(call) for call in client.calls] == [2, 1]


def test_tag_merge_emitter_passthrough():
    emitter, client, out = make_emitter()
    emitter.emit({"Id": "1", "Arn": "arn:a:1", "Tags": {"Env": "prod"}})  # has tags
    emitter.emit({"Id": "2", "Tags": {}})  # no arn
    emitter.emit("not-a-dict")
    emitter.flush()
    assert client.calls == []
    assert len(out.getvalue().splitlines()) == 3
