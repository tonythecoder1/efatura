"""Password hashing and short-lived signed session tokens."""

import base64
import hashlib
import hmac
import json
import secrets
import time


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(derived).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, salt_value, digest_value = encoded.split("$", 2)
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_value.encode())
        expected = base64.urlsafe_b64decode(digest_value.encode())
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode_segment(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_token(user_id: str, secret: str, expires_in: int) -> str:
    header = _segment(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _segment(
        json.dumps({"sub": user_id, "exp": int(time.time()) + expires_in}, separators=(",", ":")).encode()
    )
    unsigned = f"{header}.{payload}".encode()
    signature = hmac.new(secret.encode(), unsigned, hashlib.sha256).digest()
    return f"{header}.{payload}.{_segment(signature)}"


def decode_token(token: str, secret: str) -> str | None:
    try:
        header, payload, signature = token.split(".", 2)
        unsigned = f"{header}.{payload}".encode()
        expected = _segment(hmac.new(secret.encode(), unsigned, hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(_decode_segment(payload))
        if int(data.get("exp", 0)) < int(time.time()):
            return None
        subject = data.get("sub")
        return subject if isinstance(subject, str) and subject else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
