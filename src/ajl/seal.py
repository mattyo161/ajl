"""Field-level value sealing for sensitive strings (SSM SecureString, ...).

Two *orthogonal* protections guard secrets in ajl; this module is the second:

- **Cache-at-rest** — the whole gzipped cache file is age-encrypted when a key
  is configured (see cache.py). ssm + --cache requires a key, so cached
  secrets are always encrypted on disk regardless of anything here.
- **Output/pipe safety** (this module) — individual secret *values* are
  age-sealed inline in the JSONL, so a stream piped to a file, `tiss sd`, a
  log, or a screen never carries plaintext secrets. Independent of the cache:
  it protects the stdout stream itself.

A sealed value is a one-line self-describing string:

    AJLSEC:1:<base64(age ciphertext)>

The prefix makes it obviously-not-plaintext to a human, survives JSON, jq,
pipes and save/restore round-trips, and lets ``ajl --decrypt`` find sealed
fields by scan without knowing the schema. Sealing uses the same
AJL_AGE_* configuration as the cache (age recipients/identity or passphrase),
so one key covers both layers.
"""

import base64

from .cache import decrypt_bytes, encrypt_bytes, encryption_mode

PREFIX = "AJLSEC:1:"


def sealing_available():
    """True when an age recipient/passphrase is configured to seal with."""
    return encryption_mode() is not None


def is_sealed(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def seal_value(plaintext):
    """plaintext str -> 'AJLSEC:1:<b64>'. Idempotent on already-sealed input."""
    if plaintext is None or is_sealed(plaintext):
        return plaintext
    ciphertext = encrypt_bytes(str(plaintext).encode())
    return PREFIX + base64.b64encode(ciphertext).decode()


def unseal_value(sealed):
    """'AJLSEC:1:<b64>' -> plaintext str. Passes non-sealed values through."""
    if not is_sealed(sealed):
        return sealed
    ciphertext = base64.b64decode(sealed[len(PREFIX):])
    return decrypt_bytes(ciphertext).decode()


def unseal_obj(obj):
    """Recursively unseal every sealed string in a parsed JSON value."""
    if is_sealed(obj):
        return unseal_value(obj)
    if isinstance(obj, dict):
        return {key: unseal_obj(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [unseal_obj(item) for item in obj]
    return obj
