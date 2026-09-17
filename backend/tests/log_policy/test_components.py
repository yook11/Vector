"""deny は allow で解除できない。"""

import pytest

from app.log_policy.registry import PolicyRegistry
from app.log_policy.rules import LogPolicy, LogPolicyRules

pytestmark = pytest.mark.unit


def test_registered_deny_cannot_be_allowed() -> None:
    """deny に入っているキーは allow に書いても許可されない。"""
    with pytest.raises(ValueError, match="allow が deny と重複"):
        PolicyRegistry([LogPolicyRules(LogPolicy.AI_INFERENCE, frozenset({"body"}))])
