"""Loading of per-service model files that drive output shaping.

Model files live in the package (``ajl/models/<service>.json``) and can be
overridden for development with the ``AJL_MODELS_DIR`` environment variable.
"""

import json
import os
import sys
from importlib import resources

from .debug import cache_hit

_CACHE = {}


def load_service_model(service: str):
    """Return the parsed model for a service, or None when there is none."""
    if service in _CACHE:
        cache_hit("model", service)
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


_OP_INDEX = {}


def get_operation_config(service: str, operation_pascal: str):
    """Return the operation config for a service operation, or None.

    Lookup is case-insensitive: the CLI's kebab-to-Pascal conversion cannot
    reconstruct acronym casing (list-open-id-connect-providers becomes
    ListOpenIdConnectProviders, but the API name is ListOpenIDConnectProviders,
    and likewise DBInstances, WebACLs, ...).
    """
    model = load_service_model(service)
    if not model:
        return None
    operations = model.get("operations") or {}
    config = operations.get(operation_pascal)
    if config is None:
        index = _OP_INDEX.get(service)
        if index is None:
            index = {name.lower(): name for name in operations}
            _OP_INDEX[service] = index
        actual = index.get(operation_pascal.lower())
        if actual:
            config = operations[actual]
    return config
