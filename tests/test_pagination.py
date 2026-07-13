from ajl.pagination import _marker_pairs, iter_pages


class FakeMarkerClient:
    """Client without a botocore paginator; pages chained by NextToken."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def can_paginate(self, operation):
        return False

    def list_things(self, **params):
        self.calls.append(params)
        index = 0
        if "NextToken" in params:
            index = int(params["NextToken"])
        return self.pages[index]


OPERATION_CFG = {
    "input": {"markers": ["NextToken"]},
    "output": {"markers": ["NextToken"]},
}


def test_marker_pairs_next_prefix_mapping():
    cfg = {
        "input": {"markers": ["KeyMarker", "VersionIdMarker"]},
        "output": {"markers": ["NextKeyMarker", "NextVersionIdMarker"]},
    }
    assert _marker_pairs(cfg) == {
        "NextKeyMarker": "KeyMarker",
        "NextVersionIdMarker": "VersionIdMarker",
    }


def test_iter_pages_marker_fallback():
    pages = [
        {"Things": [1], "NextToken": "1"},
        {"Things": [2], "NextToken": "2"},
        {"Things": [3]},
    ]
    client = FakeMarkerClient(pages)
    result = list(iter_pages(client, "list_things", {"Foo": "bar"}, OPERATION_CFG))
    assert [p["Things"] for p in result] == [[1], [2], [3]]
    assert client.calls[1] == {"Foo": "bar", "NextToken": "1"}


def test_iter_pages_no_paginate_single_call():
    pages = [{"Things": [1], "NextToken": "1"}]
    client = FakeMarkerClient(pages)
    result = list(iter_pages(client, "list_things", {}, OPERATION_CFG, paginate=False))
    assert len(result) == 1
    assert len(client.calls) == 1
