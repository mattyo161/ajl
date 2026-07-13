"""Response pagination.

Prefers botocore's generated paginators (they exist for virtually every
list/describe operation and adapt automatically to new APIs). Falls back to a
marker loop driven by the ``input.markers`` / ``output.markers`` metadata in
the service model files for operations botocore cannot paginate.
"""

import sys


def _marker_pairs(operation_cfg):
    """Map output marker fields to the input params they feed."""
    if not operation_cfg:
        return {}
    in_markers = set((operation_cfg.get("input") or {}).get("markers") or [])
    out_markers = (operation_cfg.get("output") or {}).get("markers") or []
    pairs = {}
    for out_marker in out_markers:
        if out_marker in in_markers:
            pairs[out_marker] = out_marker
        elif out_marker.startswith("Next") and out_marker[4:] in in_markers:
            pairs[out_marker] = out_marker[4:]
    return pairs


def iter_pages(client, operation_snake, params, operation_cfg=None, paginate=True, verbose=False):
    """Yield response pages for an operation, following pagination."""
    if paginate and client.can_paginate(operation_snake):
        paginator = client.get_paginator(operation_snake)
        yield from paginator.paginate(**params)
        return

    method = getattr(client, operation_snake)
    response = method(**params)
    yield response
    if not paginate:
        return

    marker_pairs = _marker_pairs(operation_cfg)
    if not marker_pairs:
        return
    if verbose:
        print(f"ajl: marker pagination via {marker_pairs}", file=sys.stderr)
    while True:
        next_params = {}
        for out_marker, in_marker in marker_pairs.items():
            value = response.get(out_marker)
            if value:
                next_params[in_marker] = value
        if not next_params:
            return
        response = method(**{**params, **next_params})
        yield response
