"""Password hashing: scrypt (n=2^15, r=8, p=1, 16-byte salt), constant-time checks.

Stored as ``scrypt$32768$8$1$<salt>$<hash>`` (URL-safe base64, no padding). A
password never appears in a log, an event or an error message; only this
module sees it, and only long enough to hash it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

__all__ = ["MAX_PASSWORD_LENGTH", "MIN_PASSWORD_LENGTH", "PasswordRejected", "hash_password", "verify_password",
           "check_password_rules", "dummy_verify"]

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256  # hashing is deliberately expensive: refuse megabyte "passwords"
N, R, P = 2**15, 8, 1
SALT_BYTES = 16
KEY_BYTES = 32
_MAXMEM = 64 * 1024 * 1024  # scrypt needs 128*r*n = 32 MiB; OpenSSL's default cap is exactly that


class PasswordRejected(ValueError):
    """The password breaks a rule. The message is owner-facing."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def check_password_rules(password: object) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordRejected(f"Use at least {MIN_PASSWORD_LENGTH} characters for your password.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordRejected("That password is too long.")
    if "\x00" in password:
        raise PasswordRejected("That password has a character I can't use.")
    return password


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=KEY_BYTES)


def hash_password(password: str) -> str:
    check_password_rules(password)
    salt = os.urandom(SALT_BYTES)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(_derive(password, salt, N, R, P))}"


def verify_password(password: object, stored: str) -> bool:
    """True when ``password`` matches ``stored``. Never raises on bad input; compares in constant time."""
    if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_LENGTH:
        dummy_verify()
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        n_, r_, p_ = int(n), int(r), int(p)
        if not (2 <= n_ <= 2**20 and 1 <= r_ <= 32 and 1 <= p_ <= 16):
            return False
        expected = _unb64(digest)
        actual = _derive(password, _unb64(salt), n_, r_, p_)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


_DUMMY = None


def dummy_verify() -> None:
    """Spend the same time as a real check, so an unknown email answers as slowly as a known one."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = f"scrypt${N}${R}${P}${_b64(b'0' * SALT_BYTES)}${_b64(b'1' * KEY_BYTES)}"
    verify_password("not-the-password", _DUMMY)
