"""People who use the app: password hashes, signed session cookies, and encryption of broker secrets."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken

SESSION_COOKIE = "upstocks_session"
SESSION_DAYS = 30
STATE_SECONDS = 15 * 60


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
    except ValueError:
        return False                                  # '!' = no password set yet
    if scheme != "scrypt":
        return False
    actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(actual, base64.b64decode(digest))


class Signer:
    """HMAC-signed values: session cookies and the OAuth `state` that ties a broker callback to its user."""

    def __init__(self, secret: str):
        self.key = secret.encode()

    def _mac(self, text: str) -> str:
        return hmac.new(self.key, text.encode(), hashlib.sha256).hexdigest()[:32]

    def sign(self, user_id: int, seconds: int, purpose: str) -> str:
        body = f"{purpose}.{user_id}.{int(time.time()) + seconds}.{secrets.token_hex(4)}"
        return f"{body}.{self._mac(body)}"

    def verify(self, value: str | None, purpose: str) -> int | None:
        try:
            got_purpose, user_id, expires, nonce, mac = (value or "").split(".")
        except ValueError:
            return None
        body = f"{got_purpose}.{user_id}.{expires}.{nonce}"
        if got_purpose != purpose or not hmac.compare_digest(mac, self._mac(body)) or int(expires) < time.time():
            return None
        return int(user_id)

    def session(self, user_id: int) -> str:
        return self.sign(user_id, SESSION_DAYS * 86400, "s")

    def state(self, user_id: int) -> str:
        return self.sign(user_id, STATE_SECONDS, "o")


class Vault:
    """Encrypts broker API secrets and access tokens at rest (key file beside the database, mode 600)."""

    def __init__(self, key_path: Path):
        key_path = Path(key_path)
        if not key_path.exists():
            key_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        self.fernet = Fernet(key_path.read_bytes().strip())

    def seal(self, plain: str) -> str:
        return self.fernet.encrypt(plain.encode()).decode() if plain else ""

    def open(self, sealed: str) -> str:
        if not sealed:
            return ""
        try:
            return self.fernet.decrypt(sealed.encode()).decode()
        except InvalidToken:
            return ""
