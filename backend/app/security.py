import hashlib
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet
from .config import get_settings
from .logging_config import register_secret

_hasher = PasswordHasher()

def hash_password(value: str) -> str:
    return _hasher.hash(value)

def verify_password(value: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, value)
    except (VerificationError, InvalidHashError):
        return False

def encrypt_secret(value: str) -> str:
    register_secret(value)
    return Fernet(get_settings().encryption_key.encode()).encrypt(value.encode()).decode()

def decrypt_secret(value: str) -> str:
    secret = Fernet(get_settings().encryption_key.encode()).decrypt(value.encode()).decode()
    register_secret(secret)
    return secret

def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
