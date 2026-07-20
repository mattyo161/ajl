"""Tag enrichment via the Resource Groups Tagging API (--fetch-tags).

Resources that already carry tags pass straight through. Resources with an
Arn but no tags are buffered into batches of up to 100 ARNs; each batch is a
single ``GetResources(ResourceARNList=...)`` call run on a background thread
so tag fetching overlaps with fetching the next API pages. Batches are
emitted in submission order.
"""

import sys
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .normalize import tags_to_map

BATCH_SIZE = 100


def fetch_tags_for_arns(tagging_client, arns):
    """Return {arn: tags_map} for up to 100 ARNs."""
    result = {}
    params = {"ResourceARNList": list(arns)}
    while True:
        try:
            response = tagging_client.get_resources(**params)
        except Exception as exc:  # best effort: resources keep empty Tags
            print(f"ajl: fetch-tags GetResources failed: {exc}", file=sys.stderr)
            return result
        for mapping in response.get("ResourceTagMappingList") or []:
            result[mapping["ResourceARN"]] = tags_to_map(mapping.get("Tags"))
        token = response.get("PaginationToken")
        if not token:
            return result
        params["PaginationToken"] = token


class TagMergeEmitter:
    """Emitter wrapper that enriches Tag-less resources before emitting.

    ``get_tagging_client(session_key)`` supplies a resourcegroupstaggingapi
    client for the session that produced the record, so mixed-account/-region
    streams batch correctly.
    """

    def __init__(self, emitter, get_tagging_client, workers=4, batch_size=BATCH_SIZE):
        self.emitter = emitter
        self.get_tagging_client = get_tagging_client
        self.batch_size = batch_size
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.buffers = {}  # session_key -> [record, ...]
        self.pending = deque()  # futures in submission order
        self.lock = threading.Lock()

    def emit(self, record, session_key=None):
        ajl_meta = record.get("ajl") if isinstance(record, dict) else None
        if (
            not isinstance(record, dict)
            or not ajl_meta
            or ajl_meta.get("tags")
            or not ajl_meta.get("arn")
        ):
            self.emitter.emit(record)
        else:
            with self.lock:
                buffer = self.buffers.setdefault(session_key, [])
                buffer.append(record)
                if len(buffer) >= self.batch_size:
                    self._submit(session_key)
        self._drain(block=False)

    def _submit(self, session_key):
        # caller holds self.lock
        batch = self.buffers.pop(session_key, [])
        if batch:
            self.pending.append(
                self.pool.submit(self._fetch_and_merge, session_key, batch)
            )

    def _fetch_and_merge(self, session_key, batch):
        client = self.get_tagging_client(session_key)
        merged = {}
        arns = list({record["ajl"]["arn"] for record in batch})
        for start in range(0, len(arns), self.batch_size):
            merged.update(fetch_tags_for_arns(client, arns[start : start + self.batch_size]))
        for record in batch:
            ajl_meta = record["ajl"]
            ajl_meta["tags"] = merged.get(ajl_meta["arn"], {})
            if not ajl_meta.get("name") and ajl_meta["tags"].get("Name"):
                ajl_meta["name"] = ajl_meta["tags"]["Name"]
        return batch

    def _drain(self, block):
        while True:
            with self.lock:
                if not self.pending or not (block or self.pending[0].done()):
                    return
                future = self.pending.popleft()
            for record in future.result():
                self.emitter.emit(record)

    def flush(self):
        with self.lock:
            for session_key in list(self.buffers):
                self._submit(session_key)
        self._drain(block=True)
