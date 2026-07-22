"""Symmetric encryption of SSH secrets (passwords / private keys) at rest.

Secrets are stored in ``data.json`` as ``"v1:<fernet-token>"``. The ``v1:``
version prefix lets us distinguish encrypted values from legacy plaintext,
so existing deployments keep working until ``scripts/migrate_secrets.py``
re-encrypts them in place.

The Fernet key comes from ``config.MASTER_KEY`` (validated at boot). Losing
that key makes every encrypted secret unrecoverable — back it up.
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

import config

logger = logging.getLogger(__name__)

# Version prefix stamped on every encrypted value. Bump only when the scheme
# changes (e.g. new algorithm) and add a matching branch in decrypt_secret.
_PREFIX = "v1:"
_FERNET: Fernet | None = None


class SecretError(Exception):
    """Raised when a stored secret cannot be decrypted (bad key / corrupted)."""


def _get_fernet() -> Fernet:
    """Return a cached Fernet instance built from config.MASTER_KEY.

    config.py validates MASTER_KEY at import time, so by the time we get here
    it is guaranteed non-empty and well-formed.
    """
    global _FERNET
    if _FERNET is None:
        _FERNET = Fernet(config.MASTER_KEY.encode())
    return _FERNET


def encrypt_secret(plaintext: str | None) -> str:
    """Encrypt a secret, returning ``"v1:<token>"``.

    Empty / falsy input is returned unchanged so we don't pollute data.json
    with tokens for blank fields (e.g. an unused ``private_key``). Already
    encrypted values (``v1:`` prefix) are returned as-is, making the call
    idempotent and safe to run repeatedly during migration.
    """
    if not plaintext:
        return ""
    if plaintext.startswith(_PREFIX):
        return plaintext
    token = _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def decrypt_secret(value: str | None) -> str:
    """Decrypt a stored secret.

    Handling:
      * ``None`` / empty → ``""``.
      * ``v1:<token>``  → decrypted plaintext.
      * anything else   → returned as-is (legacy plaintext, pre-migration).

    Legacy passthrough is intentional: existing deployments store plaintext,
    and this keeps them readable until the migration script re-encrypts them.
    """
    if not value:
        return ""
    if not value.startswith(_PREFIX):
        return value
    token = value[len(_PREFIX):]
    try:
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise SecretError(
            "Failed to decrypt a stored secret — MASTER_KEY mismatch or "
            "corrupted value. Restore the original MASTER_KEY or re-enter "
            "the server credentials."
        ) from exc


def is_encrypted(value: str | None) -> bool:
    """True if *value* carries the encryption version prefix."""
    return bool(value) and value.startswith(_PREFIX)
