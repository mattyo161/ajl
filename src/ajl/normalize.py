"""Normalization of API responses into consistent JSONL resources.

Every emitted resource carries the raw API response fields exactly as
boto3 returned them — no renaming, no dropped fields — plus one trailing
metadata object, ``ajl``, appended last:

``ajl.type``  - ARN-style resource type, e.g. ``ec2:instance``
``ajl.id``    - the resource id (falls back to the last segment of the arn)
``ajl.name``  - the resource name (falls back to ``Tags.Name``)
``ajl.arn``   - taken from a response field or built from an ``arn_format`` template
``ajl.tags``  - a ``{Key: Value}`` map converted from the API's tag list
``ajl.uri``   - present only when ``uri_format`` is configured (s3 ``s3://bucket/key``)

Because ``ajl`` is its own namespace, a raw response field can never
collide with it — ssm's own parameter ``Type`` (``String``/
``SecureString``), a VPN gateway's ``Type``, ElastiCache's all-caps
``ARN`` all pass through untouched, sitting right next to ``ajl.type``/
``ajl.arn`` with no rename needed.

The shaping is driven by a declarative ``resources`` config on the operation
in the service model file::

    "output": {
      "resources": [
        {
          "path": ["Reservations", "Instances"],
          "type": "ec2:instance",
          "id": "InstanceId",
          "name": null,
          "arn": null,
          "arn_format": "arn:{partition}:ec2:{region}:{account}:instance/{InstanceId}",
          "tags": "Tags",
          "scalar_as": null
        }
      ]
    }

``path`` walks the response; every list along the way is iterated, every dict
is descended into. ``scalar_as`` wraps scalar list items (e.g. dynamodb
ListTables' TableNames) into ``{scalar_as: value}`` objects. ``arn_format``
may reference ``{partition}``, ``{region}``, ``{account}``, any field of the
resource, and any scalar field of the response root as ``{root_<Field>}``.
``uri_format`` (same template variables) adds ``ajl.uri``.

A hand-written ``output.jq`` program on the operation always wins over the
declarative config (the escape hatch for odd APIs).
"""


def tags_to_map(tags):
    """Convert AWS tag lists ([{Key, Value}, ...]) to a plain map."""
    if not tags:
        return {}
    if isinstance(tags, dict):
        return dict(tags)
    result = {}
    if isinstance(tags, list):
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            # ecs-style tag lists use lowercase key/value
            key = tag.get("Key", tag.get("TagKey", tag.get("key")))
            if key is not None:
                result[key] = tag.get("Value", tag.get("TagValue", tag.get("value")))
    return result


def iter_path(node, path):
    """Yield every node reached by walking ``path``, iterating lists."""
    if not path:
        yield node
        return
    if not isinstance(node, dict):
        return
    value = node.get(path[0])
    if value is None:
        return
    if isinstance(value, list):
        for item in value:
            yield from iter_path(item, path[1:])
    else:
        yield from iter_path(value, path[1:])


class _ArnVars(dict):
    """format_map source that treats None/complex values as missing."""

    def __missing__(self, key):
        raise KeyError(key)

    def __getitem__(self, key):
        value = super().__getitem__(key)
        if value is None or isinstance(value, (dict, list)):
            raise KeyError(key)
        return value


def id_from_arn(arn):
    """The resource id fallback used when a record has no id field of its
    own: the last segment of its Arn. Public (not just an internal helper)
    because run_describe_chain (main.py) reapplies it when a Get* response
    doesn't echo back the identifier it was fetched with."""
    if not arn or ":" not in arn:
        return ""
    resource_part = arn.split(":", 5)[-1]
    # resource may be "type/id", "type:id" or just "id"; take the last segment
    return resource_part.replace(":", "/").split("/")[-1]


def normalize_resource(item, cfg, context, root):
    """Return ``item`` with a trailing ``ajl`` metadata object appended."""
    if not isinstance(item, dict):
        item = {cfg.get("scalar_as") or "Value": item}

    tags_field = cfg.get("tags")
    if not tags_field and isinstance(item.get("Tags"), (list, dict)):
        tags_field = "Tags"
    tags = tags_to_map(item.get(tags_field)) if tags_field else {}

    arn_vars = None
    if cfg.get("arn_format") or cfg.get("uri_format"):
        arn_vars = _ArnVars(context)
        for key, value in (root or {}).items():
            if not isinstance(value, (dict, list)):
                arn_vars[f"root_{key}"] = value
        arn_vars.update(item)

    arn = ""
    arn_field = cfg.get("arn")
    if arn_field:
        arn = item.get(arn_field) or ""
    elif cfg.get("arn_format"):
        try:
            arn = cfg["arn_format"].format_map(arn_vars)
        except (KeyError, IndexError):
            arn = ""

    uri = ""
    if cfg.get("uri_format"):
        try:
            uri = cfg["uri_format"].format_map(arn_vars)
        except (KeyError, IndexError):
            uri = ""

    resource_id = ""
    if cfg.get("id"):
        resource_id = item.get(cfg["id"]) or ""
    if not resource_id:
        resource_id = id_from_arn(arn)

    name = ""
    if cfg.get("name"):
        name = item.get(cfg["name"]) or ""
    if not name:
        name = tags.get("Name") or ""

    ajl = {
        "type": cfg.get("type") or "",
        "id": resource_id,
        "name": name,
        "arn": arn,
        "tags": tags,
    }
    if uri:
        ajl["uri"] = uri

    result = {}
    for key, value in item.items():
        if key == tags_field:
            continue  # replaced by ajl.tags
        result[key] = value
    result["ajl"] = ajl
    return result


def iter_configured_resources(page, resource_cfgs, context):
    """Yield normalized resources for every resources config entry."""
    for cfg in resource_cfgs:
        path = cfg.get("path") or []
        for item in iter_path(page, path):
            yield normalize_resource(item, cfg, context, page)


def iter_default_resources(page):
    """Fallback shaping when an operation has no config: if the response has
    exactly one top-level list, stream its items; otherwise emit the page."""
    lists = [value for value in page.values() if isinstance(value, list)]
    if len(lists) == 1:
        yield from lists[0]
    else:
        yield page
