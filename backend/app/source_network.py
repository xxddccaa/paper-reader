"""Platform-owned source download settings, shared by API and workers."""
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import PROJECT_DIR


class SourceNetwork(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proxy_enabled: bool = False
    proxy_url: str = Field(default="", max_length=2048)
    timeout_seconds: int = Field(default=120, ge=10, le=300)

    @field_validator("proxy_url")
    @classmethod
    def validate_proxy(cls, value):
        value = value.strip()
        if not value:
            return ""
        try:
            parsed = urlsplit(value)
            port = parsed.port
            valid = (parsed.scheme == "http" and parsed.hostname and parsed.username is None
                     and parsed.password is None and parsed.path in {"", "/"}
                     and not parsed.query and not parsed.fragment and port != 0
                     and not any(c.isspace() or ord(c) < 32 for c in value)
                     and "\\" not in value and "%" not in parsed.hostname)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Use an HTTP proxy URL without credentials, path, query or fragment")
        return value.rstrip("/")

    @model_validator(mode="after")
    def enabled_requires_url(self):
        if self.proxy_enabled and not self.proxy_url:
            raise ValueError("A proxy address is required when enabled")
        return self


def network_path() -> Path:
    # storage is shared and writable in both local and container deployments;
    # deployment config/app.env itself is intentionally not web-editable.
    runtime = Path(os.environ.get("PAPER_READER_RUNTIME_DIR", PROJECT_DIR.parent / "paper-reader-runtime"))
    return runtime.expanduser().resolve() / "storage" / "config" / "source-network.json"


def load_source_network() -> SourceNetwork:
    try:
        return SourceNetwork.model_validate_json(network_path().read_text())
    except FileNotFoundError:
        return SourceNetwork()
    except (OSError, ValueError):
        # Never silently bypass a configured proxy on a malformed config.
        raise ValueError("Source network settings could not be read; save them again in platform settings") from None


def save_source_network(settings: SourceNetwork) -> SourceNetwork:
    path = network_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".source-network-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(settings.model_dump(), indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return settings
