"""Parameter Store wrappers: ``ajl ssm get`` (and the ``ssm params`` alias).

``ssm get`` picks the API from the argument, the way you'd wish the CLI did:

    --name  X            -> get_parameter            (one)
    --names A B C ...     -> get_parameters           (chunked 10/call, the API max)
    --path  /p --recursive -> get_parameters_by_path  (paginated)

``--names`` also reads newline-delimited names from stdin (``--names -``), so a
million keys stream through the worker pool ten at a time. Decryption is on by
default (``--no-decryption`` turns it off; the flag never reaches the API,
which has no such parameter — it just controls ``WithDecryption``).

SecureString values are sealed (see seal.py) so a bulk extract piped to a file
never carries plaintext secrets:

    single --name      -> plaintext output (you asked for one; --encrypt seals it)
    bulk --names/--path -> sealed output   (--decrypt to force plaintext)

Sealing needs an age recipient (AJL_AGE_*); without one, bulk get errors and
points at --decrypt. Records: Type, Name, Arn, Value, ParameterType, Version,
LastModifiedDate, DataType.
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

import jsonlines

from . import seal
from ._help import GLOBAL_FLAGS

CHUNK = 10  # GetParameters hard max names per call


def build_get_parser():
    parser = argparse.ArgumentParser(
        prog="ajl ssm get",
        description="Fetch SSM parameters; the API is chosen by --name / "
        "--names / --path. SecureStrings are sealed unless --decrypt.",
        epilog=GLOBAL_FLAGS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--name", default=None, help="one parameter (get-parameter)")
    parser.add_argument("--names", nargs="*", default=None, metavar="NAME",
                        help="many parameters (get-parameters, 10/call); '-' reads "
                        "newline-delimited names from stdin")
    parser.add_argument("--path", default=None, help="a hierarchy (get-parameters-by-path)")
    parser.add_argument("--recursive", action="store_true", default=False,
                        help="with --path: recurse into sub-paths")
    parser.add_argument("--no-decryption", action="store_true", default=False,
                        help="do not decrypt SecureStrings (WithDecryption=false)")
    parser.add_argument("--decrypt", action="store_true", default=False,
                        help="emit plaintext values (default for --name; overrides "
                        "the sealed default for --names/--path)")
    parser.add_argument("--encrypt", action="store_true", default=False,
                        help="seal SecureString values even for a single --name")
    return parser


def _shape(param):
    name = param.get("Name")
    return {
        "Type": "ssm:parameter",
        "Name": name,
        "Arn": param.get("ARN") or "",
        "Value": param.get("Value"),
        "ParameterType": param.get("Type"),
        "Version": param.get("Version"),
        "LastModifiedDate": param.get("LastModifiedDate"),
        "DataType": param.get("DataType"),
    }


def run_get(runner, emitter, options, tokens, report=None):
    opts = build_get_parser().parse_args(tokens)
    targets = [t for t in (opts.name, opts.names is not None, opts.path) if t]
    if len(targets) != 1:
        print("ajl: ssm get needs exactly one of --name / --names / --path", file=sys.stderr)
        return 2

    with_decryption = not opts.no_decryption
    single = opts.name is not None
    # single -> plaintext unless --encrypt; bulk -> sealed unless --decrypt
    do_seal = (opts.encrypt or not single) and not opts.decrypt
    if do_seal and not seal.sealing_available():
        print("ajl: ssm get seals SecureString values — configure AJL_AGE_IDENTITY "
              "(or AJL_AGE_RECIPIENTS/AJL_AGE_PASSPHRASE), or pass --decrypt for "
              "plaintext output", file=sys.stderr)
        return 2

    session_key = runner.session_key()
    client = runner.client(session_key, "ssm")
    counts = {"parameters": 0, "invalid": 0, "sealed": 0}

    def emit(param):
        record = _shape(param)
        if do_seal and record.get("ParameterType") == "SecureString" and record.get("Value"):
            record["Value"] = seal.seal_value(record["Value"])
            counts["sealed"] += 1
        emitter.emit(record, session_key)
        counts["parameters"] += 1

    try:
        if single:
            response = client.get_parameter(Name=opts.name, WithDecryption=with_decryption)
            emit(response["Parameter"])
        elif opts.path is not None:
            paginator = client.get_paginator("get_parameters_by_path")
            for page in paginator.paginate(Path=opts.path, Recursive=opts.recursive,
                                           WithDecryption=with_decryption):
                for param in page.get("Parameters") or []:
                    emit(param)
        else:
            names = _collect_names(opts.names)
            chunks = [names[i:i + CHUNK] for i in range(0, len(names), CHUNK)]
            errors = _run_chunks(client, chunks, with_decryption, emit, counts, options)
            if errors:
                if report is not None:
                    report["Stats"] = counts
                return 1
    except Exception as exc:
        print(f"ajl: ssm get failed: {exc}", file=sys.stderr)
        return 1

    if report is not None:
        report["Stats"] = counts
    if counts["invalid"]:
        return 1
    return 0


def _collect_names(names):
    if names == ["-"] or (not names and not sys.stdin.isatty()):
        return [line.strip() for line in sys.stdin if line.strip()]
    return list(names or [])


def _run_chunks(client, chunks, with_decryption, emit, counts, options):
    errors = 0
    lock = __import__("threading").Lock()

    def do_chunk(chunk):
        nonlocal errors
        try:
            response = client.get_parameters(Names=chunk, WithDecryption=with_decryption)
        except Exception as exc:
            with lock:
                errors += 1
            print(f"ajl: get-parameters chunk failed: {exc}", file=sys.stderr)
            return
        with lock:
            for param in response.get("Parameters") or []:
                emit(param)
            for bad in response.get("InvalidParameters") or []:
                counts["invalid"] += 1
                print(f"ajl: invalid parameter (not found or no access): {bad}", file=sys.stderr)

    workers = max(1, options.workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(do_chunk, chunks))
    return errors


def build_write_parser(command):
    common = argparse.ArgumentParser(
        prog=f"ajl ssm {command}",
        description=(
            "Update existing parameters by name+value, preserving Type/KeyId/"
            "Tier (skip-if-unchanged)." if command == "update" else
            "Create/overwrite parameters with an explicit Type/KeyId."),
        epilog="Bulk: stream records via --params-json - ({Name, Value, ...} "
        "per line; sealed AJLSEC values are unsealed before writing).\n\n"
        + GLOBAL_FLAGS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    common.add_argument("--name", default=None)
    common.add_argument("--value", default=None, help="value, or '-' to read stdin")
    if command == "update":
        common.add_argument("--force", action="store_true", default=False,
                            help="write even when the value is unchanged")
    else:
        common.add_argument("--type", default="String",
                            choices=["String", "StringList", "SecureString"])
        common.add_argument("--key-id", default=None, help="KMS key for SecureString")
        common.add_argument("--tier", default=None,
                            choices=["Standard", "Advanced", "Intelligent-Tiering"])
        common.add_argument("--description", default=None)
        common.add_argument("--overwrite", action="store_true", default=False,
                            help="replace an existing parameter (PutParameter Overwrite)")
    return common


def _describe_one(client, name):
    response = client.describe_parameters(
        ParameterFilters=[{"Key": "Name", "Values": [name]}], MaxResults=1)
    params = response.get("Parameters") or []
    return params[0] if params else None


def run_write(runner, emitter, options, tokens, mode, report=None):
    opts = build_write_parser(mode).parse_args(tokens)
    session_key = runner.session_key()
    client = runner.client(session_key, "ssm")
    counts = {"written": 0, "unchanged": 0, "errors": 0}
    lock = __import__("threading").Lock()

    def write_one(spec):
        name = spec.get("Name") or spec.get("name")
        value = spec.get("Value") or spec.get("value")
        if value is not None:
            value = seal.unseal_value(value)  # accept sealed values piped back in
        if not name or value is None:
            raise ValueError("each write needs a Name and Value")
        if mode == "update":
            existing = _describe_one(client, name)
            if not existing:
                raise ValueError(f"{name} does not exist — use `ssm put` to create it")
            if not opts.force:
                current = client.get_parameter(Name=name, WithDecryption=True)["Parameter"]
                if current.get("Value") == value:
                    with lock:
                        counts["unchanged"] += 1
                    return {"Type": "ssm:parameter", "Name": name, "Action": "unchanged",
                            "Version": current.get("Version")}
            put = {"Name": name, "Value": value, "Type": existing["Type"], "Overwrite": True}
            if existing["Type"] == "SecureString" and existing.get("KeyId"):
                put["KeyId"] = existing["KeyId"]
            if existing.get("Tier"):
                put["Tier"] = existing["Tier"]
            action = "updated"
        else:
            put = {"Name": name, "Value": value,
                   "Type": spec.get("Type") or spec.get("type") or opts.type,
                   "Overwrite": bool(spec.get("Overwrite", opts.overwrite))}
            key_id = spec.get("KeyId") or opts.key_id
            if key_id:
                put["KeyId"] = key_id
            if opts.tier:
                put["Tier"] = opts.tier
            if opts.description:
                put["Description"] = opts.description
            action = "put"
        response = client.put_parameter(**put)
        with lock:
            counts["written"] += 1
        return {"Type": "ssm:parameter", "Name": name, "Action": action,
                "Version": response.get("Version"), "Tier": response.get("Tier")}

    def run_spec(spec):
        try:
            emitter.emit(write_one(spec), session_key)
        except Exception as exc:
            with lock:
                counts["errors"] += 1
            print(f"ajl: ssm {mode} failed ({spec.get('Name') or spec.get('name')}): {exc}",
                  file=sys.stderr)

    if options.params_json:
        source = sys.stdin if options.params_json == "-" else open(options.params_json)
        specs = [s for s in jsonlines.Reader(source) if isinstance(s, dict)]
        workers = max(1, options.workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(run_spec, specs))
    else:
        value = opts.value
        if value == "-":
            value = sys.stdin.read().rstrip("\n")
        run_spec({"Name": opts.name, "Value": value})

    if report is not None:
        report["Stats"] = counts
    return 1 if counts["errors"] else 0


def run_decrypt_filter(options):
    """Standalone `ajl --decrypt`: unseal AJLSEC envelopes in a JSONL stream
    from stdin. For `rd secrets | ajl --decrypt` and similar."""
    import orjson

    out = sys.stdout
    with jsonlines.Reader(sys.stdin) as reader:
        for obj in reader:
            out.write(orjson.dumps(seal.unseal_obj(obj), default=str).decode() + "\n")
    out.flush()
    return 0
