#!/bin/sh
set -e

# When AWP_DEV=1, config.py synthesises ephemeral keys — don't block dev
# workflows just because secrets aren't set in the environment.
if [ "${AWP_DEV:-0}" = "1" ]; then
    exec "$@"
fi

if [ -z "${SECRET_KEY:-}" ]; then
    echo "ERROR: SECRET_KEY is not set." >&2
    echo "  Copy .env.example to .env and fill in SECRET_KEY and MASTER_KEY." >&2
    echo "  Or set AWP_DEV=1 for local development (never in production)." >&2
    exit 1
fi

if [ -z "${MASTER_KEY:-}" ]; then
    echo "ERROR: MASTER_KEY is not set." >&2
    echo "  Generate one with:" >&2
    echo "    python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"" >&2
    exit 1
fi

exec "$@"
