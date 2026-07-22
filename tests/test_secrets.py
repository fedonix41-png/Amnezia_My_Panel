import os

os.environ.setdefault("AWP_DEV", "1")

import pytest

from managers.secrets import (
    SecretError,
    decrypt_secret,
    encrypt_secret,
    is_encrypted,
)


class TestEncryptDecrypt:
    def test_round_trip_password(self):
        pw = "my-secret-password"
        ct = encrypt_secret(pw)
        assert ct != pw
        assert ct.startswith("v1:")
        assert decrypt_secret(ct) == pw

    def test_round_trip_private_key(self):
        pk = "-----BEGIN OPENSSH PRIVATE KEY-----\nline1\nline2\n-----END-----"
        ct = encrypt_secret(pk)
        assert decrypt_secret(ct) == pk

    def test_legacy_plaintext_passthrough(self):
        assert decrypt_secret("old-plaintext") == "old-plaintext"
        assert is_encrypted("old-plaintext") is False

    def test_empty_handling(self):
        assert encrypt_secret("") == ""
        assert encrypt_secret(None) == ""
        assert decrypt_secret("") == ""
        assert decrypt_secret(None) == ""

    def test_idempotency(self):
        ct = encrypt_secret("secret123")
        assert encrypt_secret(ct) == ct

    def test_v1_detection(self):
        assert is_encrypted("v1:abc123") is True
        assert is_encrypted("v1:") is True
        assert is_encrypted("plain") is False
        assert is_encrypted("") is False
        assert is_encrypted(None) is False

    def test_bad_token_raises_secret_error(self):
        with pytest.raises(SecretError):
            decrypt_secret("v1:not-valid-fernet-token!!!")

    def test_known_value_with_ephemeral_key(self):
        ct = encrypt_secret("test")
        assert is_encrypted(ct)
        assert decrypt_secret(ct) == "test"
