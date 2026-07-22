"""Centralized configuration loaded from environment / .env.

All secrets and tunables are resolved once at import time. Missing *required*
secrets cause a hard failure (``SystemExit``) unless ``AWP_DEV=1`` is set, in
which case throwaway values are synthesized for local development only.

Importing this module triggers ``load_dotenv()``, so it should be imported
before anything else that reads ``os.environ``.
"""

import logging
import os
import sys
import secrets as _secrets

logger = logging.getLogger(__name__)

# Load .env as early as possible. python-dotenv is already a project dependency.
try:  # pragma: no cover - environment dependent
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001
    # dotenv optional — env vars set directly still work.
    pass


def _truthy(value, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _split_csv(value: str):
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Dev mode
# ---------------------------------------------------------------------------
AWP_DEV = _truthy(os.environ.get("AWP_DEV"), default=False)

# ---------------------------------------------------------------------------
# SECRET_KEY — signs session cookies. MUST be stable across restarts.
# ---------------------------------------------------------------------------
SECRET_KEY = (os.environ.get("SECRET_KEY") or "").strip()
_MIN_SECRET_LEN = 32

if not SECRET_KEY:
    if AWP_DEV:
        SECRET_KEY = _secrets.token_urlsafe(48)
        logger.warning(
            "AWP_DEV=1 and SECRET_KEY not set — synthesized an EPHEMERAL key. "
            "Sessions will NOT survive a restart. Never use this in production."
        )
    else:
        logger.error(
            "SECRET_KEY is not set. Refusing to start. "
            "Set it in your .env (see .env.example) or set AWP_DEV=1 for local dev only."
        )
        sys.exit(1)
elif len(SECRET_KEY) < _MIN_SECRET_LEN:
    if AWP_DEV:
        logger.warning(
            "SECRET_KEY is shorter than %d chars — accepted only because AWP_DEV=1.",
            _MIN_SECRET_LEN,
        )
    else:
        logger.error(
            "SECRET_KEY is too short (min %d chars). Refusing to start. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\"",
            _MIN_SECRET_LEN,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# MASTER_KEY — Fernet key for encrypting SSH secrets at rest.
# Validated here so the failure surfaces at boot, not on first use.
# ---------------------------------------------------------------------------
MASTER_KEY = (os.environ.get("MASTER_KEY") or "").strip()

if AWP_DEV and not MASTER_KEY:
    # Synthesize an ephemeral key so local dev works without configuration.
    try:
        from cryptography.fernet import Fernet
    except ImportError:  # pragma: no cover - cryptography is a hard dependency
        Fernet = None  # type: ignore[assignment]
    if Fernet is not None:
        MASTER_KEY = Fernet.generate_key().decode()
        logger.warning(
            "AWP_DEV=1 and MASTER_KEY not set — synthesized an EPHEMERAL Fernet "
            "key. Already-encrypted secrets from a prior run will be unreadable."
        )
elif not MASTER_KEY:
    logger.error(
        "MASTER_KEY is not set. Refusing to start. "
        "Generate one with: python -c \"from cryptography.fernet import Fernet; "
        "print(Fernet.generate_key().decode())\""
    )
    sys.exit(1)
else:
    # Validate the key format eagerly.
    try:
        from cryptography.fernet import Fernet

        Fernet(MASTER_KEY.encode())
    except Exception as exc:  # noqa: BLE001
        logger.error("MASTER_KEY is set but invalid: %s. Refusing to start.", exc)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------
APP_PORT = int(os.environ.get("APP_PORT", "5000") or 5000)

TRUSTED_HOSTS = _split_csv(os.environ.get("TRUSTED_HOSTS", "localhost,127.0.0.1"))

# ---------------------------------------------------------------------------
# Session cookie
# ---------------------------------------------------------------------------
SESSION_COOKIE_SECURE = _truthy(os.environ.get("SESSION_COOKIE_SECURE"), default=False)
SESSION_COOKIE_SAMESITE = (os.environ.get("SESSION_COOKIE_SAMESITE") or "lax").strip().lower()
if SESSION_COOKIE_SAMESITE not in ("lax", "strict", "none"):
    SESSION_COOKIE_SAMESITE = "lax"

# ---------------------------------------------------------------------------
# Rate limiting (slowapi) — kept as raw strings for the limiter.
# ---------------------------------------------------------------------------
# Slowapi rate-limit key function: use X-Forwarded-For when behind a proxy
# (Cloudflare Tunnel, nginx), fall back to the direct client IP.
def _rate_limit_key(request):
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    if request.client:
        return request.client.host
    return "127.0.0.1"

LOGIN_RATE_LIMIT = os.environ.get("LOGIN_RATE_LIMIT", "5/minute")
SHARE_RATE_LIMIT = os.environ.get("SHARE_RATE_LIMIT", "10/minute")
CAPTCHA_RATE_LIMIT = os.environ.get("CAPTCHA_RATE_LIMIT", "30/minute")

# ---------------------------------------------------------------------------
# Automatic data.json backups
# ---------------------------------------------------------------------------
BACKUP_INTERVAL_HOURS = max(0, int(os.environ.get("BACKUP_INTERVAL_HOURS", "6") or 6))
BACKUP_KEEP_COUNT = max(1, int(os.environ.get("BACKUP_KEEP_COUNT", "14") or 14))
