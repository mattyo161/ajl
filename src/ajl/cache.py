"""Invocation result cache (the tiss cacheExec idea, built in).

``--cache 15m`` (or ``AJL_CACHE=15m``) caches a whole invocation's JSONL
output, gzipped, under ``~/.cache/ajl`` keyed by a hash of everything that
shapes the output: ajl version, service/operation, remaining CLI tokens, the
output-affecting global flags, resolved profile/region, and the full content
of any ``--params-json`` input. A hit younger than the TTL replays the stream
with zero API calls (and zero credentials); ``--refresh`` skips the read but
still writes. Only successful runs (exit 0) are stored.

Encryption (optional, applies to cache files): set ``AJL_AGE_IDENTITY`` to an
age identity (the ``AGE-SECRET-KEY-...`` value or a path to an identity file)
and both directions work — the recipient is derived. ``AJL_AGE_RECIPIENTS``
(comma/space separated ``age1...``) enables write-only encryption, and
``AJL_AGE_PASSPHRASE`` selects symmetric mode. Encrypted entries the current
keys cannot open are treated as misses — losing a session key just means the
command runs again. Note: age encrypt/decrypt buffers the (gzipped) payload
in memory; skip ``--cache`` for truly giant scans.

Retention: every entry records an expiry (``--rm-after``, default
``AJL_CACHE_RM_AFTER`` or 7d) and any cache-enabled invocation opportunistically
deletes expired entries. ``ajl cache ls|clear|keygen`` manage the cache.
"""

import gzip
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

import orjson

DEFAULT_RM_AFTER = "7d"
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text):
    """'90' -> 90s; '15m', '2h', '7d' likewise. Returns seconds (float)."""
    text = str(text).strip().lower()
    if not text:
        raise ValueError("empty duration")
    unit = 1
    if text[-1] in _UNITS:
        unit = _UNITS[text[-1]]
        text = text[:-1]
    return float(text) * unit


def cache_dir():
    return os.environ.get("AJL_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "ajl"
    )


def _human_age(seconds):
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 129600:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# --- age encryption -------------------------------------------------------

def _load_identity():
    value = os.environ.get("AJL_AGE_IDENTITY", "").strip()
    if not value:
        return None
    if not value.startswith("AGE-SECRET-KEY-"):
        with open(os.path.expanduser(value)) as fp:
            lines = [ln.strip() for ln in fp if ln.strip().startswith("AGE-SECRET-KEY-")]
        if not lines:
            raise ValueError(f"no AGE-SECRET-KEY found in {value}")
        value = lines[0]
    from pyrage import x25519

    return x25519.Identity.from_str(value)


def _load_recipients():
    from pyrage import x25519

    raw = os.environ.get("AJL_AGE_RECIPIENTS", "")
    recipients = [x25519.Recipient.from_str(r) for r in raw.replace(",", " ").split() if r]
    if recipients:
        return recipients
    identity = _load_identity()
    if identity is not None:
        return [identity.to_public()]
    return []


def encryption_mode():
    """'age', 'passphrase' or None, from the environment."""
    if os.environ.get("AJL_AGE_RECIPIENTS") or os.environ.get("AJL_AGE_IDENTITY"):
        return "age"
    if os.environ.get("AJL_AGE_PASSPHRASE"):
        return "passphrase"
    return None


def encrypt_bytes(data):
    import pyrage

    if encryption_mode() == "passphrase":
        return pyrage.passphrase.encrypt(data, os.environ["AJL_AGE_PASSPHRASE"])
    recipients = _load_recipients()
    if not recipients:
        raise ValueError("no age recipients configured")
    return pyrage.encrypt(data, recipients)


def decrypt_bytes(data):
    import pyrage

    if os.environ.get("AJL_AGE_PASSPHRASE"):
        try:
            return pyrage.passphrase.decrypt(data, os.environ["AJL_AGE_PASSPHRASE"])
        except Exception:
            pass
    identity = _load_identity()
    if identity is None:
        raise ValueError("no age identity configured")
    return pyrage.decrypt(data, [identity])


# --- the cache ------------------------------------------------------------

class ResultCache:
    """One invocation's view of the on-disk cache. ``enabled`` is False when
    neither --cache nor AJL_CACHE asked for it; every method is a no-op then.
    """

    def __init__(self, options):
        ttl_text = options.cache or os.environ.get("AJL_CACHE") or ""
        self.enabled = bool(ttl_text) and not getattr(options, "no_cache", False)
        self.ttl = parse_duration(ttl_text) if self.enabled else 0
        self.refresh = getattr(options, "refresh", False)
        rm_after = (getattr(options, "rm_after", None)
                    or os.environ.get("AJL_CACHE_RM_AFTER") or DEFAULT_RM_AFTER)
        self.rm_after = parse_duration(rm_after)
        self.directory = cache_dir()
        self.params_sha = None
        self._stdin_spool = None

    # -- key ---------------------------------------------------------------

    def buffer_params_stdin(self, options):
        """--params-json - must be read up front so it can be hashed; the
        spooled copy transparently replaces stdin for the actual run."""
        if not self.enabled or options.params_json != "-":
            return
        content = sys.stdin.read()
        self.params_sha = hashlib.sha256(content.encode()).hexdigest()
        fd, path = tempfile.mkstemp(prefix="ajl-params-", suffix=".jsonl")
        with os.fdopen(fd, "w") as fp:
            fp.write(content)
        self._stdin_spool = path
        options.params_json = path

    def cleanup_spool(self):
        if self._stdin_spool:
            try:
                os.unlink(self._stdin_spool)
            except OSError:
                pass

    def key(self, options, service, operation, tokens):
        from . import __version__

        if options.params_json and self.params_sha is None:
            with open(options.params_json, "rb") as fp:
                self.params_sha = hashlib.sha256(fp.read()).hexdigest()
        material = {
            "version": __version__,
            "service": service,
            "operation": operation,
            "tokens": list(tokens),
            "profile": options.profile,
            "region": options.region,
            "no_parse": options.no_parse,
            "no_paginate": options.no_paginate,
            "max_items": options.max_items,
            "fetch_tags": options.fetch_tags,
            "jq": options.jq,
            "stamp_session": options.stamp_session,
            "params_sha": self.params_sha,
        }
        return hashlib.sha256(orjson.dumps(material)).hexdigest()[:32]

    # -- paths & metadata ----------------------------------------------------

    def _paths(self, key):
        base = os.path.join(self.directory, key)
        return base + ".jsonl.gz", base + ".jsonl.gz.age", base + ".meta.json"

    def _read_meta(self, meta_path):
        try:
            with open(meta_path) as fp:
                return json.load(fp)
        except (OSError, ValueError):
            return None

    # -- read side -----------------------------------------------------------

    def try_replay(self, key):
        """Stream a fresh cached result to stdout. Returns the meta dict on a
        hit, None on any kind of miss (absent, stale, undecryptable)."""
        if not self.enabled or self.refresh:
            return None
        plain, encrypted, meta_path = self._paths(key)
        meta = self._read_meta(meta_path)
        if not meta:
            return None
        age = time.time() - meta.get("created", 0)
        if age > self.ttl:
            return None
        try:
            if os.path.exists(encrypted):
                with open(encrypted, "rb") as fp:
                    payload = gzip.decompress(decrypt_bytes(fp.read()))
                sys.stdout.buffer.write(payload)
                sys.stdout.flush()
            elif os.path.exists(plain):
                with gzip.open(plain, "rb") as src:
                    shutil.copyfileobj(src, sys.stdout.buffer)
                sys.stdout.flush()
            else:
                return None
        except Exception as exc:  # bad key, corrupt file: rerun instead
            print(f"ajl: cache unreadable ({exc}); re-running", file=sys.stderr)
            return None
        print(
            f"ajl: cache hit (age {_human_age(age)}, {meta.get('lines', '?')} lines, "
            f"saved {meta.get('duration', 0):.1f}s) {meta_path[:-10]}",
            file=sys.stderr,
        )
        return meta

    # -- write side ----------------------------------------------------------

    def open_writer(self, key, argv):
        if not self.enabled:
            return None
        os.makedirs(self.directory, exist_ok=True)
        self.sweep()
        return CacheWriter(self, key, argv)

    def sweep(self):
        """Delete entries past their recorded expiry (rmAfter)."""
        now = time.time()
        try:
            names = os.listdir(self.directory)
        except OSError:
            return
        for name in names:
            if not name.endswith(".meta.json"):
                continue
            meta_path = os.path.join(self.directory, name)
            meta = self._read_meta(meta_path)
            if meta and meta.get("expires", 0) > now:
                continue
            base = meta_path[: -len(".meta.json")]
            for path in (base + ".jsonl.gz", base + ".jsonl.gz.age", meta_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass


class CacheWriter:
    """Tee stream: lines go to real stdout and into a gzip temp file; commit()
    finalizes (encrypting when configured) atomically, abort() discards."""

    def __init__(self, cache, key, argv):
        self.cache = cache
        self.key = key
        self.argv = list(argv)
        self.stdout = sys.stdout
        self.lines = 0
        fd, self.tmp_path = tempfile.mkstemp(prefix="ajl-cache-", dir=cache.directory)
        self.gz = gzip.open(os.fdopen(fd, "wb"), "wb", compresslevel=6)
        self._done = False

    # file-like interface for Emitter
    def write(self, text):
        self.stdout.write(text)
        self.lines += text.count("\n")
        self.gz.write(text.encode())

    def flush(self):
        self.stdout.flush()

    def commit(self, duration):
        if self._done:
            return
        self._done = True
        self.gz.close()
        plain, encrypted, meta_path = self.cache._paths(self.key)
        try:
            if encryption_mode():
                with open(self.tmp_path, "rb") as fp:
                    sealed = encrypt_bytes(fp.read())
                with open(self.tmp_path, "wb") as fp:
                    fp.write(sealed)
                target = encrypted
                for stale in (plain,):
                    if os.path.exists(stale):
                        os.unlink(stale)
            else:
                target = plain
                for stale in (encrypted,):
                    if os.path.exists(stale):
                        os.unlink(stale)
            os.replace(self.tmp_path, target)
        except Exception as exc:
            print(f"ajl: cache write failed: {exc}", file=sys.stderr)
            self.abort()
            return
        now = time.time()
        meta = {
            "key": self.key,
            "created": now,
            "expires": now + self.cache.rm_after,
            "argv": self.argv,
            "duration": round(duration, 3),
            "lines": self.lines,
            "encrypted": bool(encryption_mode()),
        }
        with open(meta_path, "w") as fp:
            json.dump(meta, fp)

    def abort(self):
        if not self._done:
            self._done = True
            self.gz.close()
        try:
            os.unlink(self.tmp_path)
        except OSError:
            pass


# --- ajl cache <keygen|ls|clear> -------------------------------------------

def run_cache_command(operation, tokens):
    if operation == "keygen":
        from pyrage import x25519

        identity = x25519.Identity.generate()
        print(f"# public key: {identity.to_public()}")
        print(str(identity))
        print(
            "ajl: export AJL_AGE_IDENTITY='AGE-SECRET-KEY-...' to encrypt+decrypt "
            "the result cache with this key", file=sys.stderr,
        )
        return 0
    directory = cache_dir()
    if operation == "ls":
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            names = []
        now = time.time()
        for name in names:
            if not name.endswith(".meta.json"):
                continue
            meta_path = os.path.join(directory, name)
            try:
                with open(meta_path) as fp:
                    meta = json.load(fp)
            except (OSError, ValueError):
                continue
            base = meta_path[: -len(".meta.json")]
            data = next((p for p in (base + ".jsonl.gz.age", base + ".jsonl.gz")
                         if os.path.exists(p)), None)
            record = {
                "Key": meta.get("key"),
                "Command": "ajl " + " ".join(meta.get("argv") or []),
                "Age": _human_age(now - meta.get("created", now)),
                "ExpiresIn": _human_age(max(0, meta.get("expires", now) - now)),
                "Lines": meta.get("lines"),
                "Bytes": os.path.getsize(data) if data else 0,
                "Encrypted": meta.get("encrypted", False),
                "DurationS": meta.get("duration"),
            }
            print(orjson.dumps(record).decode())
        return 0
    if operation == "clear":
        remove_all = "--all" in tokens
        removed = 0
        try:
            names = os.listdir(directory)
        except OSError:
            names = []
        now = time.time()
        for name in names:
            if not name.endswith(".meta.json"):
                continue
            meta_path = os.path.join(directory, name)
            try:
                with open(meta_path) as fp:
                    meta = json.load(fp)
            except (OSError, ValueError):
                meta = {}
            if not remove_all and meta.get("expires", 0) > now:
                continue
            base = meta_path[: -len(".meta.json")]
            for path in (base + ".jsonl.gz", base + ".jsonl.gz.age", meta_path):
                try:
                    os.unlink(path)
                    removed += 1
                except OSError:
                    pass
        print(f"ajl: cache clear removed {removed} files from {directory}",
              file=sys.stderr)
        return 0
    print(f"ajl: unknown cache command {operation!r} (keygen|ls|clear)", file=sys.stderr)
    return 2
