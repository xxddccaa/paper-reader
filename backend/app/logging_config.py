"""Operational logs stay in runtime and redact values registered at secret boundaries."""
import logging
from logging.handlers import RotatingFileHandler
import os
import re
from .config import get_settings

_secrets: set[str] = set()

def register_secret(value: str):
    if value:
        _secrets.add(value)

def redact(value: str) -> str:
    for secret in sorted(_secrets, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)(bearer\s+)[^\s,;\"']+", r"\1[REDACTED]", value)
    value = re.sub(r'(?i)((?:api[_-]?key|password|encrypted_api_key)\s*[=:]\s*)[^\s,;]+', r'\1[REDACTED]', value)
    return value

class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))

def configure_logging(component: str):
    if not re.fullmatch(r"[a-z-]+", component):
        raise ValueError("Invalid log component")
    settings = get_settings()
    register_secret(settings.encryption_key)
    register_secret(settings.database_url)
    for name, value in os.environ.items():
        if any(token in name.upper() for token in ("API_KEY", "PASSWORD", "ENCRYPTION_KEY")):
            register_secret(value)
    directory = settings.runtime_dir / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / f"{component}.log"
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename == str(log_path):
            return
    handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    log_path.chmod(0o600)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
