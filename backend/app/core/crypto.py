"""Authenticated encryption for stored secrets (ADR 0018).

AES-256-GCM (via `cryptography`) with a 96-bit random nonce per encryption. The associated data
binds a ciphertext to what it belongs to (a secret's name, a user's MFA secret), so ciphertexts
cannot be swapped between rows. The master key never touches the database; only a short key id
(a hash prefix) is stored next to each ciphertext so a wrong or rotated key is detected.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings, get_settings

KEY_BYTES = 32
NONCE_BYTES = 12


class CryptoError(RuntimeError):
    pass


@dataclass(frozen=True)
class Sealed:
    nonce: bytes
    ciphertext: bytes
    key_id: str


def generate_key() -> str:
    """A new master key, base64 (url-safe) encoded."""
    return base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode()


def decode_key(value: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(value.strip().encode())
    except (ValueError, TypeError) as exc:
        raise CryptoError("master key is not valid base64") from exc
    if len(raw) != KEY_BYTES:
        raise CryptoError(f"master key must be {KEY_BYTES} bytes, got {len(raw)}")
    return raw


def key_id(key: bytes) -> str:
    return hashlib.sha256(b"erag-key-id:" + key).hexdigest()[:16]


def write_key_file(path: Path, value: str) -> None:
    """Create a key file readable only by its owner. Refuses to overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value + "\n")


def load_master_key(settings: Settings | None = None) -> bytes:
    s = settings or get_settings()
    if s.secrets_key is not None and s.secrets_key.get_secret_value():
        return decode_key(s.secrets_key.get_secret_value())
    if s.secrets_key_file is not None:
        try:
            return decode_key(s.secrets_key_file.read_text())
        except OSError as exc:
            raise CryptoError(f"cannot read ERAG_SECRETS_KEY_FILE ({s.secrets_key_file}): {exc.strerror}") from exc
    if s.is_prod:
        raise CryptoError("no master key configured (ERAG_SECRETS_KEY or ERAG_SECRETS_KEY_FILE)")
    # Dev only: derived from the (dev) session secret so a local demo works without setup.
    return hashlib.sha256(b"erag-dev-secrets-key:" + s.secret_key.get_secret_value().encode()).digest()


class Cipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise CryptoError("invalid key length")
        self._aead = AESGCM(key)
        self.key_id = key_id(key)

    def seal(self, plaintext: bytes, aad: str) -> Sealed:
        nonce = secrets.token_bytes(NONCE_BYTES)
        return Sealed(nonce, self._aead.encrypt(nonce, plaintext, aad.encode()), self.key_id)

    def open(self, sealed: Sealed, aad: str) -> bytes:
        if sealed.key_id != self.key_id:
            raise CryptoError("ciphertext was encrypted with a different master key")
        try:
            return self._aead.decrypt(sealed.nonce, sealed.ciphertext, aad.encode())
        except InvalidTag as exc:
            raise CryptoError("ciphertext failed authentication (tampered, or wrong name/owner)") from exc


def get_cipher(settings: Settings | None = None) -> Cipher:
    return Cipher(load_master_key(settings))
