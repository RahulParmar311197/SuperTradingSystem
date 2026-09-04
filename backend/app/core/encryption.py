"""Symmetric encryption for broker credentials at rest (blueprint §11, §70:
"Broker credentials must never be stored in ... Git repository / logs /
client-side database")."""

from __future__ import annotations

import json

from cryptography.fernet import Fernet

from app.core.config import get_settings


def _fernet() -> Fernet:
    return Fernet(get_settings().credentials_encryption_key.encode())


def encrypt_credentials(credentials: dict) -> str:
    return _fernet().encrypt(json.dumps(credentials).encode()).decode()


def decrypt_credentials(token: str) -> dict:
    return json.loads(_fernet().decrypt(token.encode()).decode())
