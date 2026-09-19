"""ルールの保持と、共通設定を使ったstructlogの遅延生成を検証する。"""

from dataclasses import FrozenInstanceError
from functools import partial
from unittest.mock import Mock

import pytest
import structlog

from app.log_policy import (
    BASE_LOG_RULES,
    LogPolicy,
    LogPolicyRules,
    create_policy_logger,
    policy_logger,
)

pytestmark = pytest.mark.unit

_RULES = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"count"}))


class TestLoggerCreation:
    """完成済みルールを保持し、宣言時は実体化せず初回利用時に共通設定で生成する。"""

    def test_factory_defaults_to_completed_base_rules(self) -> None:
        """ルールを省略した出力先ロガーは完成済みの基本ルールを保持する。"""
        factory = partial(
            create_policy_logger, output_logger_factory=structlog.ReturnLoggerFactory()
        )
        assert factory().rules is BASE_LOG_RULES

    def test_factory_preserves_explicit_rules(self) -> None:
        """指定された完成済みルールを再構築せずそのまま保持する。"""
        factory = partial(
            create_policy_logger, output_logger_factory=structlog.ReturnLoggerFactory()
        )
        assert factory("test", _RULES).rules is _RULES

    def test_factory_passes_name_to_output_logger_factory(self) -> None:
        """出力先の生成方法にロガー名だけを渡す。"""
        output_factory = Mock(return_value=structlog.ReturnLogger())
        create_policy_logger(
            "article_analysis", _RULES, output_logger_factory=output_factory
        )
        output_factory.assert_called_once_with("article_analysis")

    @pytest.mark.parametrize(
        "rules", [None, "infrastructure", LogPolicy.INFRASTRUCTURE]
    )
    def test_factory_rejects_invalid_rules_before_creating_output_logger(
        self, rules
    ) -> None:
        """完成済みルール以外の指定は出力先を作る前に拒否する。"""
        output_factory = Mock()
        with pytest.raises(TypeError, match="^rules must be LogPolicyRules$"):
            create_policy_logger("test", rules, output_logger_factory=output_factory)
        output_factory.assert_not_called()

    def test_policy_logger_delegates_rendered_output(self) -> None:
        """整形済みの出力は指定した出力先ロガーへそのまま渡す。"""
        logger = create_policy_logger(
            "test", _RULES, output_logger_factory=structlog.ReturnLoggerFactory()
        )
        assert logger.info("rendered log") == "rendered log"

    def test_declared_logger_uses_factory_only_on_first_use(
        self, configure_chain
    ) -> None:
        """設定前の宣言では実体化せず、初回利用時に設定済みfactoryを呼ぶ。"""
        factory = Mock(
            wraps=partial(
                create_policy_logger,
                output_logger_factory=structlog.ReturnLoggerFactory(),
            )
        )
        logger = policy_logger("test", _RULES)
        configure_chain()
        structlog.configure(logger_factory=factory)
        factory.assert_not_called()
        logger.info("completed", count=3)
        factory.assert_called_once_with("test", _RULES)

    def test_first_use_cache_reuses_policy_logger(self, configure_chain) -> None:
        """キャッシュ有効時は繰り返し出力しても本体を作り直さない。"""
        configure_chain()
        factory = Mock(
            wraps=partial(
                create_policy_logger,
                output_logger_factory=structlog.ReturnLoggerFactory(),
            )
        )
        structlog.configure(logger_factory=factory, cache_logger_on_first_use=True)
        logger = policy_logger("test", _RULES)
        logger.info("first")
        logger.info("second")
        factory.assert_called_once_with("test", _RULES)


class TestRulesAreFixed:
    """bind・引数・contextvars・属性代入のどれでもロガーのルールを差し替えられない。"""

    def test_policy_logger_rules_cannot_be_reassigned(self) -> None:
        """生成済みロガーのルールを属性の代入で変更できない。"""
        logger = create_policy_logger(
            "test", _RULES, output_logger_factory=structlog.ReturnLoggerFactory()
        )
        with pytest.raises(FrozenInstanceError):
            logger.rules = BASE_LOG_RULES

    def test_rules_are_absent_from_event_before_policy_processing(
        self, configure_chain
    ) -> None:
        """ルールはポリシー処理で除去する前からログの辞書に入っていない。"""
        configure_chain()
        capture = structlog.testing.LogCapture()
        structlog.configure(processors=[capture])
        logger = policy_logger("test", _RULES)
        logger.info("completed", count=3)
        assert capture.entries == [
            {"event": "completed", "count": 3, "log_level": "info"}
        ]

    def test_bind_keeps_declared_rules(self, configure_chain) -> None:
        """bindで旧内部キーに別ルールを渡しても元の禁止規則を維持する。"""
        capture = configure_chain()
        rules = _RULES.extend(allow=frozenset({"count"}), deny=frozenset({"content"}))
        logger = policy_logger("test", rules).bind(_log_policy_rules=BASE_LOG_RULES)
        logger.info("completed", count=3, content="synthetic-private")
        assert capture.entries[0]["count"] == 3
        assert capture.entries[0]["_denied_keys"] == ["content"]
        assert "content" not in capture.entries[0]

    def test_call_arguments_cannot_replace_rules(self, configure_chain) -> None:
        """ログ引数に別ルールを渡してもロガー固有の禁止規則を維持する。"""
        capture = configure_chain()
        rules = _RULES.extend(allow=frozenset({"count"}), deny=frozenset({"content"}))
        logger = policy_logger("test", rules)
        logger.info(
            "completed", _log_policy_rules=BASE_LOG_RULES, content="synthetic-private"
        )
        assert capture.entries[0]["_denied_keys"] == ["content"]
        assert "content" not in capture.entries[0]

    def test_contextvars_cannot_replace_rules(self, configure_chain) -> None:
        """contextvarsに別ルールを渡してもロガー固有の禁止規則を維持する。"""
        capture = configure_chain()
        rules = _RULES.extend(allow=frozenset({"count"}), deny=frozenset({"content"}))
        structlog.contextvars.bind_contextvars(_log_policy_rules=BASE_LOG_RULES)
        logger = policy_logger("test", rules)
        logger.info("completed", content="synthetic-private")
        assert capture.entries[0]["_denied_keys"] == ["content"]
        assert "content" not in capture.entries[0]

    def test_loggers_with_same_name_keep_separate_rules(self, configure_chain) -> None:
        """名前が同じでも異なるルールを指定したロガーは互いに影響しない。"""
        capture = configure_chain()
        first_rules = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset({"first_count"})
        )
        second_rules = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset({"second_count"})
        )
        first = policy_logger("same_name", first_rules)
        second = policy_logger("same_name", second_rules)
        first.info("completed", first_count=1, second_count=2)
        second.info("completed", first_count=1, second_count=2)
        first.info("completed", first_count=1, second_count=2)
        assert capture.entries[0]["first_count"] == 1
        assert "second_count" not in capture.entries[0]
        assert capture.entries[1]["second_count"] == 2
        assert "first_count" not in capture.entries[1]
        assert capture.entries[2] == capture.entries[0] | {
            "timestamp": capture.entries[2]["timestamp"]
        }
