import pytest
from pydantic import ValidationError
from app.config import Settings


def test_tool_budget_defaults_to_160(monkeypatch):
    monkeypatch.delenv("PAPER_READER_MAX_TOOL_CALLS", raising=False)
    settings = Settings(database_url="postgresql://unused/config_test", encryption_key="unused", _env_file=None)
    assert settings.max_tool_calls == 160


@pytest.mark.parametrize("limit", [9, 501])
def test_out_of_range_tool_budget_rejected(limit):
    with pytest.raises(ValidationError, match="max_tool_calls"):
        Settings(database_url="postgresql://unused/config_test", encryption_key="unused", max_tool_calls=limit, _env_file=None)
