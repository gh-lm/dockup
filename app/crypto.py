import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from . import config

_fernet = None


def fernet():
    global _fernet
    if _fernet is None:
        digest = hashlib.sha256(config.SECRET_KEY.encode()).digest()
        _fernet = Fernet(base64.urlsafe_b64encode(digest))
    return _fernet


def encrypt(plain: str) -> str:
    return fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""
