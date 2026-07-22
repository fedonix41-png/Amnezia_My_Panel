#!/usr/bin/env python3
"""One-shot migration: encrypt plaintext SSH secrets in data.json in place.

Walks every server record and re-encrypts ``password`` / ``private_key``
fields that are not yet versioned (no ``v1:`` prefix). Already-encrypted
fields are left untouched, so the script is safe to re-run.

Behaviour:
  * Backs up the original data.json to ``data.json.bak.<timestamp>`` first.
  * Writes the migrated data atomically (temp file + os.replace).
  * Prints a summary of how many fields were converted.

Run from the project root:
    python scripts/migrate_secrets.py

Requires a valid MASTER_KEY in the environment (or .env). In AWP_DEV=1 mode
an ephemeral key is synthesized — do NOT use that against real data, the
secrets would be unreadable after the process exits.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Make the project root importable when run as a script from anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402  — triggers load_dotenv() + MASTER_KEY validation
from managers.secrets import encrypt_secret, is_encrypted  # noqa: E402

DATA_FILE = Path(os.environ.get("DATA_FILE", ROOT / "data.json"))


def _backup(source: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = source.with_name(f"{source.name}.bak.{stamp}")
    shutil.copy2(source, dest)
    return dest


def _atomic_write_json(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def migrate(path: Path = DATA_FILE) -> int:
    import json

    if not path.exists():
        print(f"[!] data file not found: {path}")
        return 1

    backup = _backup(path)
    print(f"[+] backup written: {backup}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    servers = data.get("servers", []) if isinstance(data, dict) else []
    converted_passwords = 0
    converted_keys = 0
    skipped = 0

    for srv in servers:
        if not isinstance(srv, dict):
            continue
        for field in ("password", "private_key"):
            value = srv.get(field)
            if value is None:
                continue
            if is_encrypted(value):
                skipped += 1
                continue
            if value == "":
                continue  # don't version-tag empty fields
            srv[field] = encrypt_secret(value)
            if field == "password":
                converted_passwords += 1
            else:
                converted_keys += 1

    _atomic_write_json(path, json.dumps(data, indent=2, ensure_ascii=False))

    print(
        f"[+] migration complete: {converted_passwords} password(s), "
        f"{converted_keys} private_key(s) encrypted; {skipped} already-encrypted field(s) skipped."
    )
    print(f"[+] MASTER_KEY fingerprint verified during encryption.")
    return 0


if __name__ == "__main__":
    force = "--force" in sys.argv
    if getattr(config, "AWP_DEV", False) and not force:
        print(
            "[!] AWP_DEV=1 is set — an EPHEMERAL MASTER_KEY is in use.",
            "    Encrypted secrets would be UNREADABLE after this process exits.",
            "    Set a real MASTER_KEY or re-run with --force only for throwaway test data.",
            sep="\n",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.exit(migrate())
