"""Loading of per-service model files that drive output shaping.

Model files live in the package (``ajl/models/<service>.json``) and can be
overridden for development with the ``AJL_MODELS_DIR`` environment variable.
"""

import json
import os
import sys
from importlib import resources

_CACHE = {}


def load_service_model(service: str):
    """Return the parsed model for a service, or None when there is none."""
    if service in _CACHE:
        return _CACHE[service]

    model = None
    override_dir = os.environ.get("AJL_MODELS_DIR")
    if override_dir:
        path = os.path.join(override_dir, f"{service}.json")
        if os.path.exists(path):
            with open(path) as fp:
                model = json.load(fp)
    if model is None:
        try:
            resource = resources.files("ajl").joinpath("models", f"{service}.json")
            if resource.is_file():
                model = json.loads(resource.read_text())
        except (ModuleNotFoundError, FileNotFoundError, OSError) as exc:
            print(f"ajl: unable to read model for {service}: {exc}", file=sys.stderr)

    _CACHE[service] = model
    return model


def get_operation_config(service: str, operation_pascal: str):
    """Return the operation config for a service operation, or None."""
    model = load_service_model(service)
    if not model:
        return None
    return (model.get("operations") or {}).get(operation_pascal)
