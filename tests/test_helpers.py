"""Unit tests for the pure helper functions in app.py.

These functions are security-relevant (password hashing, token generation,
config-link encoding, translation lookup) and have no HTTP/fixture
dependencies, so they are cheap to cover directly.
"""

import json
import os

os.environ.setdefault("AWP_DEV", "1")

import base64

from app import (
    _t,
    _generate_api_token,
    _maybe_migrate_legacy_data_file,
    _touch_api_token,
    generate_vpn_link,
    hash_password,
    verify_password,
)


class TestPasswordHashing:
    def test_hash_then_verify_roundtrip(self):
        h = hash_password("s3cret")
        assert verify_password("s3cret", h)

    def test_wrong_password_rejected(self):
        h = hash_password("s3cret")
        assert not verify_password("nope", h)

    def test_each_hash_has_unique_salt(self):
        # Same password → different stored hashes (random salt).
        assert hash_password("abc") != hash_password("abc")

    def test_verify_malformed_hash_returns_false(self):
        # Must not raise on garbage input — returns False defensively.
        assert not verify_password("abc", "not-a-real-hash")
        assert not verify_password("abc", "")
        assert not verify_password("abc", "salt")  # missing $ separator


class TestVpnLinkEncoding:
    def test_link_is_base64_with_scheme(self):
        link = generate_vpn_link("[Interface]\nPrivateKey=xxx")
        assert link.startswith("vpn://")
        body = link[len("vpn://"):]
        # Round-trips to the stripped original.
        decoded = base64.b64decode(body).decode("utf-8")
        assert decoded == "[Interface]\nPrivateKey=xxx"

    def test_link_strips_surrounding_whitespace(self):
        a = generate_vpn_link("payload")
        b = generate_vpn_link("  payload\n")
        assert a == b


class TestTranslations:
    def test_known_key_english(self):
        # invalid_login is present in translations/en.json.
        assert _t("invalid_login", "en")

    def test_unknown_key_falls_back_to_id(self):
        assert _t("nonexistent_key_xyz", "en") == "nonexistent_key_xyz"

    def test_unknown_lang_falls_back_to_english(self):
        # An unsupported lang must not crash — English is the safe fallback.
        result = _t("invalid_login", "xx-ZZ")
        assert result == _t("invalid_login", "en")


class TestTokenGeneration:
    def test_token_has_awp_prefix(self):
        assert _generate_api_token().startswith("awp_")

    def test_tokens_are_unique(self):
        a = _generate_api_token()
        b = _generate_api_token()
        assert a != b
        # ~256 bits beyond the prefix.
        assert len(a) > len("awp_") + 32


class TestTokenTouchThrottle:
    def test_first_touch_records_timestamp(self):
        entry = {"last_used_at": None}
        assert _touch_api_token(entry) is True
        assert entry["last_used_at"] is not None

    def test_rapid_second_touch_is_throttled(self):
        entry = {"last_used_at": None}
        _touch_api_token(entry)
        # Within the 5-minute window → caller should NOT persist again.
        assert _touch_api_token(entry) is False

    def test_corrupt_timestamp_is_overwritten(self):
        from datetime import datetime

        entry = {"last_used_at": "not-a-real-timestamp"}
        # A bad previous value must not crash; treat as stale and re-stamp.
        assert _touch_api_token(entry) is True
        # New value is a valid ISO timestamp.
        datetime.fromisoformat(entry["last_used_at"])


class TestLegacyDataFileMigration:
    """The DATA_FILE env-var fix relocated the data path. This pins the
    one-time rescue logic so an upgrade from a pre-fix deploy does not
    silently reset the panel to defaults."""

    def test_migrates_legacy_when_target_absent(self, tmp_path, monkeypatch):
        import app

        legacy = tmp_path / "legacy_data.json"
        target = tmp_path / "data" / "data.json"
        legacy.write_text(json.dumps({"users": [{"username": "rescued"}]}))

        monkeypatch.setattr(app, "DATA_FILE", str(target))
        monkeypatch.setattr(app, "_LEGACY_DATA_FILE", str(legacy))

        _maybe_migrate_legacy_data_file()

        assert target.exists()
        assert json.loads(target.read_text())["users"][0]["username"] == "rescued"

    def test_does_not_overwrite_existing_target(self, tmp_path, monkeypatch):
        import app

        legacy = tmp_path / "legacy_data.json"
        target = tmp_path / "data" / "data.json"
        # Both exist — the target is authoritative; legacy must NOT clobber it.
        legacy.write_text(json.dumps({"users": [{"username": "OLD"}]}))
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"users": [{"username": "NEW"}]}))

        monkeypatch.setattr(app, "DATA_FILE", str(target))
        monkeypatch.setattr(app, "_LEGACY_DATA_FILE", str(legacy))

        _maybe_migrate_legacy_data_file()

        assert json.loads(target.read_text())["users"][0]["username"] == "NEW"

    def test_noop_when_paths_identical(self, tmp_path, monkeypatch):
        import app

        # env var unset → DATA_FILE == legacy path → nothing to migrate.
        same = str(tmp_path / "data.json")
        monkeypatch.setattr(app, "DATA_FILE", same)
        monkeypatch.setattr(app, "_LEGACY_DATA_FILE", same)

        # Must not raise even though the file doesn't exist.
        _maybe_migrate_legacy_data_file()
        assert not os.path.exists(same)

    def test_noop_when_legacy_absent(self, tmp_path, monkeypatch):
        import app

        target = str(tmp_path / "data" / "data.json")
        legacy = str(tmp_path / "no_such_file.json")
        monkeypatch.setattr(app, "DATA_FILE", target)
        monkeypatch.setattr(app, "_LEGACY_DATA_FILE", legacy)

        _maybe_migrate_legacy_data_file()
        # Fresh install → no file created, no crash.
        assert not os.path.exists(target)

    def test_noop_when_legacy_empty(self, tmp_path, monkeypatch):
        import app

        legacy = tmp_path / "legacy_data.json"
        target = str(tmp_path / "data" / "data.json")
        legacy.write_text("")  # zero bytes — nothing worth rescuing

        monkeypatch.setattr(app, "DATA_FILE", target)
        monkeypatch.setattr(app, "_LEGACY_DATA_FILE", str(legacy))

        _maybe_migrate_legacy_data_file()
        assert not os.path.exists(target)
