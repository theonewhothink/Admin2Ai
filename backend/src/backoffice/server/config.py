"""Settings of the production API, read once from the environment.

Every variable is documented in ``.env.example``. Secrets (database password,
vault key, OAuth client secrets, GoCardless keys, Stripe keys, SMTP password, Expo and
Anthropic tokens) only ever come from the environment, which the platform
fills from its secrets manager; none has a usable default.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = ["ConfigError", "DEMO", "PRODUCTION", "ServerConfig", "mode_from_env"]

DEMO = "demo"
PRODUCTION = "production"
MIN_KEY_LENGTH = 32


class ConfigError(ValueError):
    """A setting is missing or malformed. Developer-facing (start-up), never shown to owners."""


def mode_from_env(env: Mapping[str, str] | None = None) -> str:
    """``demo`` (default) or ``production`` from ``BACKOFFICE_MODE``."""
    value = (os.environ if env is None else env).get("BACKOFFICE_MODE", DEMO).strip().lower() or DEMO
    if value not in (DEMO, PRODUCTION):
        raise ConfigError(f"BACKOFFICE_MODE must be {DEMO} or {PRODUCTION}, not {value!r}")
    return value


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _int(env: Mapping[str, str], name: str, default: int, *, low: int, high: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a whole number") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return value


def _list(env: Mapping[str, str], name: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in env.get(name, "").split(",") if v.strip())


@dataclass(frozen=True)
class ServerConfig:
    mode: str = PRODUCTION
    database_url: str = field(default="", repr=False)
    db_pool_size: int = 10
    allowed_origins: tuple[str, ...] = ()
    insecure_cookies: bool = False
    admin_emails: frozenset[str] = frozenset()
    api_url: str = "http://localhost:8000"
    web_url: str = "http://localhost:3000"
    state_key: bytes = field(default=b"", repr=False)
    vault_key: str = field(default="", repr=False)
    vault_kms_key_id: str = ""
    aws_region: str = "eu-south-2"
    s3_bucket: str = ""
    s3_prefix: str = ""
    s3_endpoint_url: str = ""
    evidence_kms_key_id: str = ""
    s3_bypass_governance: bool = False
    s3_erasure_role_arn: str = ""  # the evidence-deletion role the sync worker assumes to finish erasures
    s3_replica_bucket: str = ""  # the disaster-recovery copy of the evidence bucket (erased too)
    s3_replica_region: str = ""
    object_dir: str = "/var/lib/backoffice/objects"
    gocardless_secret_id: str = field(default="", repr=False)
    gocardless_secret_key: str = field(default="", repr=False)
    gocardless_api_url: str = ""
    expo_access_token: str = field(default="", repr=False)
    push_enabled: bool = True
    anthropic_enabled: bool = False
    max_json_bytes: int = 1024 * 1024
    tenant_cache_size: int = 200
    log_level: str = "INFO"
    strict_reads: bool = False  # a read that changes a tenant raises (tests); otherwise logged and rebuilt
    sync_interval_s: int = 900  # the sync worker: how often each connection is read
    history_days: int = 90  # the first sync of a mailbox or bank reads this far back (§6: 90, or 365)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServerConfig:
        e = os.environ if env is None else env
        mode = mode_from_env(e)
        state_key = e.get("BACKOFFICE_STATE_KEY", "")
        if state_key and len(state_key) < MIN_KEY_LENGTH:
            raise ConfigError(f"BACKOFFICE_STATE_KEY must be at least {MIN_KEY_LENGTH} characters")
        origins = _list(e, "BACKOFFICE_ALLOWED_ORIGINS")
        for origin in origins:
            if origin == "*" or not origin.startswith(("https://", "http://")) or origin.endswith("/"):
                raise ConfigError("BACKOFFICE_ALLOWED_ORIGINS lists exact origins like https://app.example.com")
        return cls(
            mode=mode,
            database_url=e.get("DATABASE_URL", "").strip(),
            db_pool_size=_int(e, "BACKOFFICE_DB_POOL_SIZE", 10, low=1, high=100),
            allowed_origins=origins,
            insecure_cookies=_flag(e, "BACKOFFICE_INSECURE_COOKIES"),
            admin_emails=frozenset(a.lower() for a in _list(e, "BACKOFFICE_ADMIN_EMAILS")),
            api_url=(e.get("BACKOFFICE_API_URL") or "http://localhost:8000").rstrip("/"),
            web_url=(e.get("BACKOFFICE_WEB_URL") or "http://localhost:3000").rstrip("/"),
            state_key=state_key.encode(),
            vault_key=e.get("BACKOFFICE_VAULT_KEY", "").strip(),
            vault_kms_key_id=e.get("BACKOFFICE_VAULT_KMS_KEY_ID", "").strip(),
            aws_region=e.get("AWS_REGION", "eu-south-2").strip() or "eu-south-2",
            s3_bucket=e.get("S3_BUCKET", "").strip(),
            s3_prefix=e.get("S3_PREFIX", "").strip(),
            s3_endpoint_url=e.get("S3_ENDPOINT_URL", "").strip(),
            evidence_kms_key_id=e.get("EVIDENCE_KMS_KEY_ID", "").strip(),
            s3_bypass_governance=_flag(e, "S3_BYPASS_GOVERNANCE_ON_ERASURE"),
            s3_erasure_role_arn=e.get("S3_ERASURE_ROLE_ARN", "").strip(),
            s3_replica_bucket=e.get("S3_REPLICA_BUCKET", "").strip(),
            s3_replica_region=e.get("S3_REPLICA_REGION", "").strip(),
            object_dir=e.get("BACKOFFICE_OBJECT_DIR", "").strip() or "/var/lib/backoffice/objects",
            gocardless_secret_id=e.get("GOCARDLESS_SECRET_ID", "").strip(),
            gocardless_secret_key=e.get("GOCARDLESS_SECRET_KEY", "").strip(),
            gocardless_api_url=e.get("GOCARDLESS_API_URL", "").strip(),
            expo_access_token=e.get("EXPO_ACCESS_TOKEN", "").strip(),
            push_enabled=not _flag(e, "BACKOFFICE_PUSH_DISABLED"),
            anthropic_enabled=bool(e.get("ANTHROPIC_API_KEY", "").strip()),
            max_json_bytes=_int(e, "BACKOFFICE_MAX_JSON_BYTES", 1024 * 1024, low=16 * 1024, high=16 * 1024 * 1024),
            tenant_cache_size=_int(e, "BACKOFFICE_TENANT_CACHE_SIZE", 200, low=1, high=100_000),
            log_level=(e.get("LOG_LEVEL") or "INFO").strip().upper(),
            strict_reads=_flag(e, "BACKOFFICE_STRICT_READS"),
            sync_interval_s=_int(e, "BACKOFFICE_SYNC_INTERVAL", 900, low=30, high=86_400),
            history_days=_int(e, "BACKOFFICE_HISTORY_DAYS", 90, low=30, high=365),
        )

    def require_production(self) -> None:
        """Refuse to start a production API that could not work or would be unsafe."""
        if not self.database_url:
            raise ConfigError("production needs DATABASE_URL (the backoffice_api login, never the superuser)")

    @property
    def secure_cookies(self) -> bool:
        return not self.insecure_cookies

    @property
    def gocardless_configured(self) -> bool:
        return bool(self.gocardless_secret_id and self.gocardless_secret_key)
