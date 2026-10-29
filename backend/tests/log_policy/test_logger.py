"""processorのルールの保持と、ログの入力や属性の代入でルールを差し替えられないことを検証する。"""

from dataclasses import FrozenInstanceError

import pytest
import structlog
from structlog.testing import LogCapture

from app.log_policy import (
    BASE_LOG_RULES,
    LogPolicy,
    LogPolicyProcessor,
    LogPolicyRules,
)

pytestmark = pytest.mark.unit

_RULES = LogPolicyRules(LogPolicy.PROCESS_RUNTIME, frozenset({"count"}))


class TestProcessorCreation:
    """完成済みルールをそのまま保持し、それ以外の指定は生成時に拒否する。"""

    def test_processor_preserves_explicit_rules(self) -> None:
        """指定された完成済みルールを再構築せずそのまま保持する。"""
        assert LogPolicyProcessor(_RULES).rules is _RULES

    @pytest.mark.parametrize(
        "rules", [None, "process_runtime", LogPolicy.PROCESS_RUNTIME]
    )
    def test_processor_rejects_invalid_rules(self, rules) -> None:
        """完成済みルール以外の指定は生成時に拒否する。"""
        with pytest.raises(TypeError, match="^rules must be LogPolicyRules$"):
            LogPolicyProcessor(rules)


class TestRulesAreFixed:
    """bind・引数・contextvars・属性代入のどれでもロガーのルールを差し替えられない。"""

    def test_processor_rules_cannot_be_reassigned(self) -> None:
        """生成済みprocessorのルールを属性の代入で変更できない。"""
        processor = LogPolicyProcessor(_RULES)
        with pytest.raises(FrozenInstanceError):
            processor.rules = BASE_LOG_RULES

    def test_bind_keeps_declared_rules(self, chain_logger) -> None:
        """bindで旧内部キーに別ルールを渡しても元の禁止規則を維持する。"""
        capture = LogCapture()
        rules = _RULES.extend(allow=frozenset({"count"}), deny=frozenset({"content"}))
        logger = chain_logger(rules, capture).bind(_log_policy_rules=BASE_LOG_RULES)
        logger.info("completed", count=3, content="synthetic-private")
        assert capture.entries[0]["count"] == 3
        assert capture.entries[0]["_denied_keys"] == ["content"]
        assert "content" not in capture.entries[0]

    def test_call_arguments_cannot_replace_rules(self, chain_logger) -> None:
        """ログ引数に別ルールを渡してもロガー固有の禁止規則を維持する。"""
        capture = LogCapture()
        rules = _RULES.extend(allow=frozenset({"count"}), deny=frozenset({"content"}))
        logger = chain_logger(rules, capture)
        logger.info(
            "completed", _log_policy_rules=BASE_LOG_RULES, content="synthetic-private"
        )
        assert capture.entries[0]["_denied_keys"] == ["content"]
        assert "content" not in capture.entries[0]

    def test_contextvars_cannot_replace_rules(self, chain_logger) -> None:
        """contextvarsに別ルールを渡してもロガー固有の禁止規則を維持する。"""
        capture = LogCapture()
        rules = _RULES.extend(allow=frozenset({"count"}), deny=frozenset({"content"}))
        structlog.contextvars.bind_contextvars(_log_policy_rules=BASE_LOG_RULES)
        logger = chain_logger(rules, capture)
        logger.info("completed", content="synthetic-private")
        assert capture.entries[0]["_denied_keys"] == ["content"]
        assert "content" not in capture.entries[0]

    def test_loggers_keep_separate_rules(self, chain_logger) -> None:
        """異なるルールで作ったロガーは、同じ出力先へ交互に出しても互いに影響しない。"""
        capture = LogCapture()
        first_rules = LogPolicyRules(
            LogPolicy.PROCESS_RUNTIME, frozenset({"first_count"})
        )
        second_rules = LogPolicyRules(
            LogPolicy.PROCESS_RUNTIME, frozenset({"second_count"})
        )
        first = chain_logger(first_rules, capture)
        second = chain_logger(second_rules, capture)
        first.info("completed", first_count=1, second_count=2)
        second.info("completed", first_count=1, second_count=2)
        first.info("completed", first_count=1, second_count=2)
        assert capture.entries[0]["first_count"] == 1
        assert "second_count" not in capture.entries[0]
        assert capture.entries[1]["second_count"] == 2
        assert "first_count" not in capture.entries[1]
        assert capture.entries[2] == {
            **capture.entries[0],
            "timestamp": capture.entries[2]["timestamp"],
        }
