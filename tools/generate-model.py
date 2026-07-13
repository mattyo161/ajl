#!/usr/bin/env python3
"""Generate ajl model files from botocore's bundled service definitions.

Unlike the aws-sdk-go extraction, this needs no network or git clone: it reads
the service-2.json / paginators-1.json data shipped with the installed boto3,
so the models always match what the client will actually accept.

Usage:
    python3 tools/generate-model.py lambda iam sns ...
    python3 tools/generate-model.py --all-existing   # regenerate current files

The output schema matches the existing model files (metadata, name,
operations with input/output members, types and pagination markers). Run
tools/apply-resource-configs.py afterwards to re-apply the curated output
shaping.
"""

import argparse
import json
import os
import re
import sys

import botocore.session

MODELS_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "ajl", "models")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


def _as_marker_list(value):
    """Normalize a paginator token (str or list) to plain member names."""
    if not value:
        return []
    values = value if isinstance(value, list) else [value]
    # skip jmespath expressions like "NextMarker || Contents[-1].Key";
    # botocore's paginator handles those itself
    return [v for v in values if _IDENTIFIER.match(v)]


def _members(shapes, shape_name):
    if not shape_name:
        return {"members": {}, "required": None}
    shape = shapes.get(shape_name) or {}
    members = {}
    for name, ref in (shape.get("members") or {}).items():
        member_shape = ref.get("shape")
        members[name] = {
            "name": name,
            "shape_name": member_shape,
            "type": (shapes.get(member_shape) or {}).get("type"),
        }
    return {"members": members, "required": shape.get("required")}


def generate_model(loader, service):
    service_2 = loader.load_service_model(service, "service-2")
    try:
        pagination = loader.load_service_model(service, "paginators-1").get("pagination") or {}
    except Exception:
        pagination = {}

    shapes = service_2.get("shapes") or {}
    operations = {}
    for op_name, op in (service_2.get("operations") or {}).items():
        paginator = pagination.get(op_name) or {}
        input_cfg = _members(shapes, (op.get("input") or {}).get("shape"))
        output_cfg = _members(shapes, (op.get("output") or {}).get("shape"))
        input_cfg["markers"] = _as_marker_list(paginator.get("input_token")) or None
        output_cfg["markers"] = _as_marker_list(paginator.get("output_token")) or None
        operations[op_name] = {
            "name": op_name,
            "input": input_cfg,
            "output": output_cfg,
        }

    return {
        "metadata": service_2.get("metadata") or {},
        "name": service,
        "operations": operations,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("services", nargs="*", help="boto3 service names")
    parser.add_argument("--all-existing", action="store_true",
                        help="regenerate every model file already present")
    args = parser.parse_args()

    services = list(args.services)
    if args.all_existing:
        services += [
            f[:-5] for f in os.listdir(MODELS_DIR)
            if f.endswith(".json") and f[:-5] not in services
        ]
    if not services:
        parser.error("no services given")

    loader = botocore.session.get_session().get_component("data_loader")
    for service in sorted(services):
        try:
            model = generate_model(loader, service)
        except Exception as exc:
            print(f"{service}: FAILED ({exc})", file=sys.stderr)
            continue
        path = os.path.join(MODELS_DIR, f"{service}.json")
        with open(path, "w") as fp:
            json.dump(model, fp, indent=2, sort_keys=True)
            fp.write("\n")
        print(f"{service}: {len(model['operations'])} operations", file=sys.stderr)


if __name__ == "__main__":
    main()
