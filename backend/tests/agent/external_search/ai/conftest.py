"""Shared fixtures for external search DeepSeek adapter tests."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.config import settings


@pytest.fixture(autouse=True)
def set_deepseek_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", SecretStr("SECRET_API_KEY"))
