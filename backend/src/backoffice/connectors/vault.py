"""Encrypted credential vault: sign in once, stay connected (§47, §52).

Every stored secret (OAuth refresh tokens, IMAP app passwords, bank consent
ids) is sealed with envelope encryption:

* a fresh 256-bit data key per record encrypts the secret with AES-256-GCM;
* the data key is wrapped by a key provider (AWS KMS in production, a local
  master key in development) and only the wrapped key is stored;
* the tenant id, connection id and provider are bound in as associated data,
  so a record copied to another tenant or connection fails to decrypt.

Plaintext secrets never leave this module except to the connector that needs
them, never appear in ``repr`` or logs, and are never sent to the browser.

:meth:`TokenVault.token_provider` returns a
:class:`~backoffice.connectors.oauth.RefreshingTokenProvider` that renews the
access token before it expires and writes rotated refresh tokens back, so the
owner only reconnects when the provider itself revokes access.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from backoffice.domain.models import utcnow

__all__ = [
    "AwsKmsKeyProvider",
    "CredentialNotFound",
    "CredentialRecord",
    "InMemoryCredentialStore",
    "KeyProvider",
    "LocalKeyProvider",
    "TokenVault",
    "VaultError",
]


class VaultError(Exception):
    """Sealing or opening failed (wrong key, tampered record, missing crypto library)."""


class CredentialNotFound(VaultError):
    pass


def _aesgcm():  # type: ignore[no-untyped-def]
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except BaseException:  # pragma: no cover - missing or broken native library
        raise VaultError("the 'cryptography' package is required to store credentials") from None
    return AESGCM


class KeyProvider(Protocol):
    key_id: str

    def generate_data_key(self, context: dict[str, str]) -> tuple[bytes, bytes]:
        """Return ``(plaintext_key, wrapped_key)`` for a new 256-bit data key."""
        ...

    def unwrap(self, wrapped: bytes, context: dict[str, str]) -> bytes:
        ...


def _context_bytes(context: dict[str, str]) -> bytes:
    return json.dumps(context, sort_keys=True, separators=(",", ":")).encode()


class LocalKeyProvider:
    """Wraps data keys with a local 256-bit master key (development and tests)."""

    def __init__(self, master_key: bytes, key_id: str = "local-1") -> None:
        if len(master_key) != 32:
            raise VaultError("master key must be 32 bytes")
        self._key = master_key
        self.key_id = key_id

    @classmethod
    def from_env(cls, var: str = "BACKOFFICE_VAULT_KEY") -> LocalKeyProvider:
        raw = os.environ.get(var, "")
        try:
            key = base64.urlsafe_b64decode(raw.encode() + b"=" * (-len(raw) % 4))
        except ValueError:
            key = b""
        if len(key) != 32:
            raise VaultError(f"{var} must hold a base64 32-byte key")
        return cls(key)

    def generate_data_key(self, context: dict[str, str]) -> tuple[bytes, bytes]:
        data_key = os.urandom(32)
        nonce = os.urandom(12)
        wrapped = nonce + _aesgcm()(self._key).encrypt(nonce, data_key, _context_bytes(context))
        return data_key, wrapped

    def unwrap(self, wrapped: bytes, context: dict[str, str]) -> bytes:
        try:
            return _aesgcm()(self._key).decrypt(wrapped[:12], wrapped[12:], _context_bytes(context))
        except Exception:
            raise VaultError("credential key could not be unwrapped") from None


class AwsKmsKeyProvider:
    """AWS KMS envelope keys; the encryption context is enforced by KMS (§52)."""

    def __init__(self, key_id: str, *, client: Any = None, region: str = "eu-south-2") -> None:
        self.key_id = key_id
        if client is None:
            import boto3  # lazy: optional dependency

            client = boto3.client("kms", region_name=region)
        self._kms = client

    def generate_data_key(self, context: dict[str, str]) -> tuple[bytes, bytes]:
        out = self._kms.generate_data_key(KeyId=self.key_id, KeySpec="AES_256", EncryptionContext=context)
        return out["Plaintext"], out["CiphertextBlob"]

    def unwrap(self, wrapped: bytes, context: dict[str, str]) -> bytes:
        try:
            return self._kms.decrypt(CiphertextBlob=wrapped, EncryptionContext=context, KeyId=self.key_id)["Plaintext"]
        except Exception:
            raise VaultError("credential key could not be unwrapped") from None


@dataclass(frozen=True)
class CredentialRecord:
    """What is stored. Holds no plaintext secret."""

    tenant_id: str
    connection_id: str
    provider: str  # "google" | "microsoft" | "imap" | "open_banking" | "portal"
    key_id: str
    wrapped_key: bytes = field(repr=False)
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None  # when the grant itself ends (e.g. PSD2 consent), if known
    version: int = 1


class CredentialStore(Protocol):
    def put(self, record: CredentialRecord) -> None: ...
    def get(self, tenant_id: str, connection_id: str) -> CredentialRecord | None: ...
    def delete(self, tenant_id: str, connection_id: str) -> bool: ...
    def list(self, tenant_id: str) -> list[CredentialRecord]: ...


class InMemoryCredentialStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], CredentialRecord] = {}
        self._lock = threading.Lock()

    def put(self, record: CredentialRecord) -> None:
        with self._lock:
            self._rows[(record.tenant_id, record.connection_id)] = record

    def get(self, tenant_id: str, connection_id: str) -> CredentialRecord | None:
        return self._rows.get((tenant_id, connection_id))

    def delete(self, tenant_id: str, connection_id: str) -> bool:
        with self._lock:
            return self._rows.pop((tenant_id, connection_id), None) is not None

    def list(self, tenant_id: str) -> list[CredentialRecord]:
        return [r for (t, _), r in sorted(self._rows.items()) if t == tenant_id]


class TokenVault:
    """Seal, open, rotate and delete connection secrets."""

    def __init__(
        self,
        keys: KeyProvider,
        store: CredentialStore | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._keys = keys
        self._store = store or InMemoryCredentialStore()
        self._clock = clock

    @staticmethod
    def _context(tenant_id: str, connection_id: str, provider: str) -> dict[str, str]:
        return {"tenant": tenant_id, "connection": connection_id, "provider": provider}

    def store(
        self,
        tenant_id: str,
        connection_id: str,
        provider: str,
        secret: dict[str, Any],
        *,
        expires_at: datetime | None = None,
    ) -> CredentialRecord:
        for name, value in (("tenant_id", tenant_id), ("connection_id", connection_id), ("provider", provider)):
            if not isinstance(value, str) or not value.strip():
                raise VaultError(f"{name} is required")
        if not isinstance(secret, dict) or not secret:
            raise VaultError("nothing to store")
        context = self._context(tenant_id, connection_id, provider)
        data_key, wrapped = self._keys.generate_data_key(context)
        nonce = os.urandom(12)
        plaintext = json.dumps(secret, sort_keys=True, default=str).encode()
        ciphertext = _aesgcm()(data_key).encrypt(nonce, plaintext, _context_bytes(context))
        now = self._clock()
        previous = self._store.get(tenant_id, connection_id)
        record = CredentialRecord(
            tenant_id=tenant_id, connection_id=connection_id, provider=provider, key_id=self._keys.key_id,
            wrapped_key=wrapped, nonce=nonce, ciphertext=ciphertext,
            created_at=previous.created_at if previous else now, updated_at=now, expires_at=expires_at,
            version=(previous.version + 1) if previous else 1,
        )
        self._store.put(record)
        return record

    def open(self, tenant_id: str, connection_id: str) -> dict[str, Any]:
        record = self._store.get(tenant_id, connection_id)
        if record is None:
            raise CredentialNotFound(connection_id)
        context = self._context(record.tenant_id, record.connection_id, record.provider)
        data_key = self._keys.unwrap(record.wrapped_key, context)
        try:
            plaintext = _aesgcm()(data_key).decrypt(record.nonce, record.ciphertext, _context_bytes(context))
        except Exception:
            raise VaultError("credential could not be opened") from None
        return json.loads(plaintext)

    def update(self, tenant_id: str, connection_id: str, changes: dict[str, Any]) -> CredentialRecord:
        record = self._store.get(tenant_id, connection_id)
        if record is None:
            raise CredentialNotFound(connection_id)
        secret = {**self.open(tenant_id, connection_id), **changes}
        return self.store(tenant_id, connection_id, record.provider, secret, expires_at=record.expires_at)

    def delete(self, tenant_id: str, connection_id: str) -> bool:
        return self._store.delete(tenant_id, connection_id)

    def has(self, tenant_id: str, connection_id: str) -> bool:
        return self._store.get(tenant_id, connection_id) is not None

    def metadata(self, tenant_id: str, connection_id: str) -> CredentialRecord | None:
        record = self._store.get(tenant_id, connection_id)
        return replace(record) if record else None

    def token_provider(self, tenant_id: str, connection_id: str, refresher: Any) -> Any:
        """An auto-refreshing access-token source whose rotations are saved back here."""
        from .oauth import OAuthToken, RefreshingTokenProvider  # lazy: needs httpx

        secret = self.open(tenant_id, connection_id)

        def on_rotate(token: OAuthToken) -> None:
            self.update(tenant_id, connection_id, {"refresh_token": token.refresh_token})

        return RefreshingTokenProvider(refresher, secret["refresh_token"], on_rotate=on_rotate, clock=self._clock)
