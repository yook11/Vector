"""source registry helper のテスト。"""

from __future__ import annotations

import pytest

from app.collection.sources.article_completion_policy import DEFAULT_POLICY
from app.collection.sources.errors import SourceNotRegisteredError
from app.collection.sources.registry import completion_policy_for
from app.collection.sources.source_name import SourceName


def test_completion_policy_for_returns_registered_source_policy() -> None:
    assert completion_policy_for(SourceName("TechCrunch")) is DEFAULT_POLICY


def test_completion_policy_for_raises_source_error_for_unregistered_source() -> None:
    """registry miss は KeyError ではなく source 側 typed error として通知する。"""
    with pytest.raises(SourceNotRegisteredError):
        completion_policy_for(SourceName("Definitely Missing Source"))
