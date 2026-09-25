"""実チェーンを通したログ出力の契約。合成値のみを使う。"""

from __future__ import annotations

import json
from typing import Any

import pytest
import structlog

from app.log_policy import (
    BASE_LOG_RULES,
    LogPolicy,
    LogPolicyRules,
    PolicyLogger,
    policy_logger,
    value_preparation,
)
from app.log_policy.budget import MAX_ITEMS_PER_LOG_EVENT, TEXT_LIMIT
from app.log_policy.exceptions import extraction
from app.log_policy.processor import LogPolicyProcessor
from app.log_policy.value_preparation import DEPTH_LIMIT

pytestmark = pytest.mark.unit

_SECRET = "hunter2-synthetic-secret"
# 合成値を分割し、秘密検出ツールの規則に一致させない。
_GEMINI_KEY = "AIza" + "SyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"
_PRIVATE_SAMPLE = "保護対象の合成テキスト"

_TEST_RULES = LogPolicyRules(
    policy=LogPolicy.PIPELINE_CONTROL,
    allow=frozenset({"source_id", "url", "sample_length", "payload", "items"}),
    deny=frozenset({"restricted_sample", "restricted_nested"}),
)
_INFRA_RULES = LogPolicyRules(
    policy=LogPolicy.INFRASTRUCTURE,
    allow=frozenset({"resource", "endpoint", "error_message"}),
)


@pytest.fixture
def masking_logger() -> PolicyLogger:
    rules = BASE_LOG_RULES.extend(
        allow=frozenset({"private_text", "payload"}),
        mask=frozenset({"private_text"}),
    )
    return PolicyLogger(rules, structlog.ReturnLogger())


@pytest.fixture
def sanitizing_logger() -> PolicyLogger:
    rules = LogPolicyRules(
        policy=LogPolicy.INFRASTRUCTURE,
        allow=frozenset({"payload"}),
        sanitize=frozenset({"canonical_url"}),
    )
    return PolicyLogger(rules, structlog.ReturnLogger())


class TestLoggingInputSources:
    """ログ引数・bind・contextvarsのどの入力経路にも禁止項目の除外を適用する。"""

    def test_denied_key_passed_as_argument_is_dropped(self, configure_chain) -> None:
        """ログ引数の禁止キーは値が出ず、名前だけ `_denied_keys` に残る。"""
        capture = configure_chain()
        logger = policy_logger("test", _TEST_RULES)
        logger.info("fetch_failed", source_id=1, password=_SECRET)
        entry = capture.entries[0]
        assert entry["_denied_keys"] == ["password"]
        assert _SECRET not in repr(entry)

    def test_denied_key_passed_via_bind_is_dropped(self, configure_chain) -> None:
        """bind した禁止キーも、ログ引数と同じく値が出ずに名前だけ残る。"""
        capture = configure_chain()
        logger = policy_logger("test", _TEST_RULES)
        logger.bind(api_key=_SECRET).info("fetch_failed", source_id=1)
        entry = capture.entries[0]
        assert entry["_denied_keys"] == ["api_key"]
        assert _SECRET not in repr(entry)

    def test_denied_key_passed_via_contextvars_is_dropped(
        self, configure_chain
    ) -> None:
        """contextvars の禁止キーも、ログ引数と同じく値が出ずに名前だけ残る。"""
        capture = configure_chain()
        structlog.contextvars.bind_contextvars(authorization=f"Bearer {_SECRET}")
        logger = policy_logger("test", _TEST_RULES)
        logger.info("fetch_failed", source_id=1)
        entry = capture.entries[0]
        assert entry["_denied_keys"] == ["authorization"]
        assert _SECRET not in repr(entry)


class TestRuleApplication:
    """ロガーに設定した規則を適用し、ログ入力による規則の変更を認めない。"""

    def test_logger_without_policy_drops_every_key(self, configure_chain) -> None:
        """ポリシー未宣言の logger は業務キーを出さず、未登録件数だけ残す。"""
        capture = configure_chain()
        logger = structlog.get_logger("test")
        logger.info("fetch_done", source_id=1, url="https://example.com/a")
        entry = capture.entries[0]
        assert entry["_unregistered_count"] == 2
        assert "log_policy" not in entry
        assert not {"source_id", "url"} & entry.keys()

    def test_explicit_base_rules_keep_only_base_fields(self, configure_chain) -> None:
        """基底ルールを明示したloggerは基本項目だけを残して認証情報を除外する。"""
        capture = configure_chain()
        policy_logger("test", BASE_LOG_RULES).info(
            "completed", count=3, password=_SECRET
        )
        entry = capture.entries[0]
        assert entry["event"] == "completed"
        assert entry["_denied_keys"] == ["password"]
        assert entry["_unregistered_count"] == 1
        assert "count" not in entry
        assert "password" not in entry
        assert "log_policy" not in entry

    def test_declared_policy_name_is_emitted(self, configure_chain) -> None:
        """宣言したポリシーは `log_policy` として出力に残る。"""
        capture = configure_chain()
        logger = policy_logger("test", _TEST_RULES)
        logger.info("fetch_done", source_id=1)
        assert capture.entries[0]["log_policy"] == "pipeline_control"

    def test_processor_uses_completed_rules_without_rebuilding(
        self, monkeypatch
    ) -> None:
        """ロガーが持つ完成済みの規則を繰り返し出力しても再構築しない。"""
        parent = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset(), frozenset({"restricted_sample"})
        )
        rules = parent.extend(allow=frozenset({"count"}))

        def reject_reconstruction(self):
            pytest.fail("完成済みの規則を再構築した")

        monkeypatch.setattr(LogPolicyRules, "__post_init__", reject_reconstruction)
        processor = LogPolicyProcessor()
        event = {
            "event": "completed",
            "count": 3,
            "restricted_sample": "synthetic private value",
        }
        expected = {
            "event": "completed",
            "count": 3,
            "log_policy": "infrastructure",
            "_denied_keys": ["restricted_sample"],
        }
        assert (
            processor(PolicyLogger(rules, structlog.ReturnLogger()), "info", event)
            == expected
        )
        assert (
            processor(PolicyLogger(rules, structlog.ReturnLogger()), "info", event)
            == expected
        )

    def test_loggers_with_same_policy_use_their_bound_rules(
        self, configure_chain
    ) -> None:
        """同じ識別子のlogger同士でも、それぞれが受け取った規則を適用する。"""
        first = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"first_count"}))
        last = first.extend(allow=frozenset({"last_count"}))
        capture = configure_chain()
        first_logger = policy_logger("first", first)
        last_logger = policy_logger("last", last)
        first_logger.info("completed", first_count=1, last_count=2)
        last_logger.info("completed", first_count=1, last_count=2)
        assert capture.entries[0]["first_count"] == 1
        assert "last_count" not in capture.entries[0]
        assert capture.entries[1]["last_count"] == 2
        assert "first_count" not in capture.entries[1]

    def test_policy_cannot_be_selected_from_call_site_string(
        self, configure_chain
    ) -> None:
        """呼び出し kwargs の文字列ではポリシーを選べない。"""
        capture = configure_chain()
        logger = structlog.get_logger("test")
        logger.info("fetch_done", _log_policy_rules="pipeline_control", source_id=1)
        entry = capture.entries[0]
        assert "log_policy" not in entry
        assert entry["_unregistered_count"] == 1

    def test_event_policy_identifier_does_not_select_rules(self) -> None:
        """ログ内の識別子をルールとして使わずロガーの基本ルールで保護する。"""
        processor = LogPolicyProcessor()
        output = processor(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {
                "_log_policy_rules": LogPolicy.INFRASTRUCTURE,
                "event": "completed",
                "password": "synthetic private value",
                "count": 3,
            },
        )
        assert output == {
            "event": "completed",
            "_denied_keys": ["password"],
            "_unregistered_count": 1,
        }

    def test_legacy_rule_field_never_reaches_renderer(self, configure_chain) -> None:
        """旧内部キーをallowと引数へ渡してもルールオブジェクトは出力しない。"""
        capture = configure_chain()
        rules = _TEST_RULES.extend(allow=frozenset({"_log_policy_rules"}))
        policy_logger("test", rules).info("completed", _log_policy_rules=BASE_LOG_RULES)
        assert "_log_policy_rules" not in capture.entries[0]
        assert "_unregistered_count" not in capture.entries[0]
        assert capture.entries[0]["log_policy"] == "pipeline_control"


class TestFieldSelection:
    """キー名の正規化と完全一致で項目を選別し、未登録項目を除外する。"""

    def test_camel_case_variant_of_denied_key_is_dropped(self, configure_chain) -> None:
        """`apiKey` は禁止キーとして落ち、値はログに出ない。"""
        capture = configure_chain()
        logger = policy_logger("test", _TEST_RULES)
        logger.info("fetch_failed", apiKey=_SECRET)
        entry = capture.entries[0]
        assert "apiKey" not in entry
        assert _SECRET not in repr(entry)

    def test_exact_deny_keeps_similar_allowed_key(self, configure_chain) -> None:
        """`token` の値は出ず、名前が似ていても許可した `completion_tokens` は残る。"""
        rules = LogPolicyRules(
            LogPolicy.PIPELINE_CONTROL, frozenset({"completion_tokens"})
        )
        capture = configure_chain()
        logger = policy_logger("test", rules)
        logger.info("usage", token=_SECRET, completion_tokens=128)
        entry = capture.entries[0]
        assert "token" not in entry
        assert entry["completion_tokens"] == 128
        assert _SECRET not in repr(entry)

    def test_unregistered_key_is_dropped_and_counted(self, configure_chain) -> None:
        """未登録キーは名前も値も出さず、件数だけを残す。"""
        capture = configure_chain()
        logger = policy_logger("test", _TEST_RULES)
        logger.info("fetch_done", source_id=1, elapsed_ms=12)
        entry = capture.entries[0]
        assert entry["_unregistered_count"] == 1
        assert "elapsed_ms" not in entry


class TestFieldProtection:
    """採用した値のネスト内の禁止項目・不正な構造を出力に残さない。"""

    @pytest.mark.parametrize(
        "key", ["event", "level", "timestamp", "logger", "logger_name", "loggerName"]
    )
    def test_base_fields_use_normal_value_preparation(self, key: str) -> None:
        """基本項目も通常の構造検査・deny除外・情報漏洩防止を通す。"""
        fields = {
            key: {"password": "synthetic", "message": "token=synthetic", "count": 1}
        }
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()), "info", fields
        )
        assert output == {
            key: {"message": "token=[redacted:credential]", "count": 1},
            "_denied_nested_count": 1,
        }

    def test_denied_key_nested_in_allowed_dict_is_dropped_and_counted(
        self,
        configure_chain,
    ) -> None:
        """許可した dict の中の禁止キーは落ち、名前ではなく件数だけ残る。"""
        capture = configure_chain()
        logger = policy_logger("test", _TEST_RULES)
        logger.info(
            "article_converted",
            sample_length=len(_PRIVATE_SAMPLE),
            payload={
                "id": 7,
                "restricted_sample": _PRIVATE_SAMPLE,
                "meta": {"restricted_nested": "<p>x</p>"},
            },
        )
        entry = capture.entries[0]
        assert entry["payload"] == {"id": 7, "meta": {}}
        assert entry["_denied_nested_count"] == 2
        assert entry["sample_length"] == len(_PRIVATE_SAMPLE)
        assert _PRIVATE_SAMPLE not in repr(entry)

    def test_denied_key_nested_in_list_items_is_dropped(self, configure_chain) -> None:
        """list 要素の dict にも、同じ禁止キーの除外が適用される。"""
        capture = configure_chain()
        logger = policy_logger("test", _TEST_RULES)
        logger.info(
            "feed_parsed",
            items=[{"id": 1, "restricted_sample": _PRIVATE_SAMPLE}, {"id": 2}],
        )
        entry = capture.entries[0]
        assert entry["items"] == [{"id": 1}, {"id": 2}]
        assert entry["_denied_nested_count"] == 1

    def test_nonstring_mapping_is_replaced_without_losing_other_fields(self) -> None:
        """非文字列キーを含む辞書全体を置換し、外側の正常な項目は保持する。"""
        output = LogPolicyProcessor()(
            PolicyLogger(_TEST_RULES, structlog.ReturnLogger()),
            "info",
            {
                "event": "completed",
                "payload": {200: "synthetic", "count": 1},
                "source_id": 7,
            },
        )
        assert output == {
            "event": "completed",
            "payload": "[non-string-key]",
            "source_id": 7,
            "log_policy": _TEST_RULES.policy.value,
        }


class TestMask:
    """対象項目の値全体を、型やネスト先によらず固定マーカーへ置き換える。"""

    def test_string_field_is_masked(self, masking_logger: PolicyLogger) -> None:
        """対象項目の値が文字列なら、その文字列全体をマスクする。"""
        fields = {"private_text": "synthetic private text"}

        output = LogPolicyProcessor()(masking_logger, "info", fields)

        assert output == {"private_text": "***"}

    def test_numeric_field_is_masked(self, masking_logger: PolicyLogger) -> None:
        """対象項目の値が数値でも、固定マーカーに置き換える。"""
        fields = {"private_text": 123}

        output = LogPolicyProcessor()(masking_logger, "info", fields)

        assert output == {"private_text": "***"}

    def test_dictionary_field_is_masked_as_a_whole(
        self, masking_logger: PolicyLogger
    ) -> None:
        """対象項目の値が辞書なら、辞書全体を一つの固定マーカーに置き換える。"""
        fields = {"private_text": {"message": "synthetic"}}

        output = LogPolicyProcessor()(masking_logger, "info", fields)

        assert output == {"private_text": "***"}

    def test_list_field_is_masked_as_a_whole(
        self, masking_logger: PolicyLogger
    ) -> None:
        """対象項目の値が配列なら、配列全体を一つの固定マーカーに置き換える。"""
        fields = {"private_text": ["first", "second"]}

        output = LogPolicyProcessor()(masking_logger, "info", fields)

        assert output == {"private_text": "***"}

    def test_field_in_nested_dictionary_is_masked(
        self, masking_logger: PolicyLogger
    ) -> None:
        """辞書の中に辞書がある場合でも、対象項目の値全体をマスクする。"""
        fields = {"payload": {"details": {"private_text": "synthetic"}}}

        output = LogPolicyProcessor()(masking_logger, "info", fields)

        assert output == {"payload": {"details": {"private_text": "***"}}}

    def test_field_in_dictionary_inside_list_is_masked(
        self, masking_logger: PolicyLogger
    ) -> None:
        """配列内の辞書でも、対象項目の値全体をマスクして兄弟項目を残す。"""
        fields = {
            "payload": [{"private_text": {"message": "synthetic"}, "count": 1}],
        }

        output = LogPolicyProcessor()(masking_logger, "info", fields)

        assert output == {"payload": [{"private_text": "***", "count": 1}]}

    def test_field_outside_mask_is_preserved(
        self, masking_logger: PolicyLogger
    ) -> None:
        """対象名を含む別の項目名はマスクせず、値をそのまま残す。"""
        fields = {"payload": {"private_text_suffix": "synthetic"}}

        output = LogPolicyProcessor()(masking_logger, "info", fields)

        assert output == {"payload": {"private_text_suffix": "synthetic"}}

    def test_normalized_field_name_is_matched_for_masking(
        self, masking_logger: PolicyLogger
    ) -> None:
        """項目名を正規化して対象と一致すれば、元の項目名を残して値をマスクする。"""
        fields = {"PrivateText": "synthetic"}

        output = LogPolicyProcessor()(masking_logger, "info", fields)

        assert output == {"PrivateText": "***"}


class TestSanitize:
    """辞書や配列がネストしていても、登録した項目にだけサニタイズを適用する。"""

    def test_registered_field_in_nested_dictionary_is_sanitized(
        self, sanitizing_logger: PolicyLogger
    ) -> None:
        """辞書の中に辞書がある場合でも、登録した項目をサニタイズする。"""
        fields = {
            "payload": {
                "details": {"canonical_url": "https://example.com/a/1?p=123#top"},
            },
        }

        output = LogPolicyProcessor()(sanitizing_logger, "info", fields)

        assert output["payload"] == {
            "details": {"canonical_url": "https://example.com/a/1?p=123"},
        }

    def test_registered_field_in_dictionary_inside_list_is_sanitized(
        self, sanitizing_logger: PolicyLogger
    ) -> None:
        """配列の中に辞書がある場合でも、登録した項目をサニタイズする。"""
        fields = {"payload": [{"canonical_url": "https://example.com/a/1?p=123#top"}]}

        output = LogPolicyProcessor()(sanitizing_logger, "info", fields)

        assert output["payload"] == [{"canonical_url": "https://example.com/a/1?p=123"}]

    def test_registered_field_with_nested_lists_is_sanitized(
        self, sanitizing_logger: PolicyLogger
    ) -> None:
        """登録項目の配列の中に配列がある場合でも、中の文字列をサニタイズする。"""
        fields = {
            "payload": {"canonical_url": [["https://example.com/a/1?p=123#top"]]},
        }

        output = LogPolicyProcessor()(sanitizing_logger, "info", fields)

        assert output["payload"] == {
            "canonical_url": [["https://example.com/a/1?p=123"]],
        }

    def test_unregistered_field_is_not_sanitized(
        self, sanitizing_logger: PolicyLogger
    ) -> None:
        """未登録の項目は同じ値でもサニタイズしない。"""
        fields = {"payload": {"reference": "https://example.com/a/1?p=123#top"}}

        output = LogPolicyProcessor()(sanitizing_logger, "info", fields)

        assert output["payload"] == {
            "reference": "https://example.com/a/1?p=123#top",
        }

    def test_unregistered_field_inside_registered_field_is_preserved(
        self, sanitizing_logger: PolicyLogger
    ) -> None:
        """登録項目の配下でも、辞書内の未登録項目はサニタイズしない。"""
        fields = {
            "payload": {
                "canonical_url": [{"note": "https://example.com/a/1?p=123#top"}],
            },
        }

        output = LogPolicyProcessor()(sanitizing_logger, "info", fields)

        assert output["payload"] == {
            "canonical_url": [{"note": "https://example.com/a/1?p=123#top"}],
        }

    def test_registered_top_level_field_is_matched_by_normalized_name(self) -> None:
        """トップレベルの項目名を正規化して照合し、登録項目の型不一致を置換する。"""
        rules = BASE_LOG_RULES.extend(
            allow=frozenset({"canonical_url"}),
            sanitize=frozenset({"canonical_url"}),
        )

        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"CanonicalUrl": 123},
        )

        assert output == {"CanonicalUrl": "[unsupported]"}

    def test_registered_non_string_values_in_nested_containers_are_replaced(
        self, sanitizing_logger: PolicyLogger
    ) -> None:
        """辞書と配列が入れ子でも、登録項目の配列内にある型不一致の値を置換する。"""
        fields = {"payload": {"details": [{"canonical_url": [[123]]}]}}

        output = LogPolicyProcessor()(sanitizing_logger, "info", fields)

        assert output["payload"] == {
            "details": [{"canonical_url": [["[unsupported]"]]}],
        }


class TestLeakPrevention:
    """採用した値・ネストのキー・例外・診断の文字列に、最後に情報漏洩防止を適用する。"""

    def test_credential_inside_allowed_text_is_redacted(self, configure_chain) -> None:
        """許可した自由文に混入した credential だけを置き換え、原因文は残す。"""
        capture = configure_chain()
        logger = policy_logger("test", _INFRA_RULES)
        logger.warning(
            "db_connect_failed",
            error_message=(
                f"connection to postgresql://vector:{_SECRET}@db:5432/v refused"
            ),
        )
        message = capture.entries[0]["error_message"]
        assert _SECRET not in message
        assert message == (
            "connection to postgresql://[redacted:url_userinfo]@db:5432/v refused"
        )

    def test_event_string_is_redacted(self, configure_chain) -> None:
        """event 文字列に混入した credential も置換される。"""
        capture = configure_chain()
        logger = policy_logger("test", _TEST_RULES)
        logger.info(f"failed with Authorization: Bearer {_SECRET}abcdef0123456789")
        assert capture.entries[0]["event"] == (
            "failed with Authorization: [redacted:credential]"
        )

    def test_nested_keys_and_list_values_are_redacted(self) -> None:
        """ネストの辞書キーと配列内の文字列にも、情報漏洩防止を適用する。"""
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"payload"}))

        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"payload": {"token=synthetic": ["token=other"]}},
        )

        assert output["payload"] == {
            "token=[redacted:credential]": ["token=[redacted:credential]"],
        }

    def test_leak_prevention_runs_after_sanitize(
        self, sanitizing_logger: PolicyLogger
    ) -> None:
        """サニタイズで残した部分にも、情報漏洩防止を適用する。"""
        fields = {
            "payload": {
                "canonical_url": f"https://example.com/a/1?key={_GEMINI_KEY}#top",
            },
        }

        output = LogPolicyProcessor()(sanitizing_logger, "info", fields)

        assert output["payload"] == {
            "canonical_url": "https://example.com/a/1?key=[redacted:gemini_api_key]",
        }

    def test_exception_message_is_redacted_in_output(self) -> None:
        """例外メッセージ内のAPIキーを置き換え、周囲の原因文を残して出力する。"""
        message = f"request with {_GEMINI_KEY} failed"
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "error",
            {"event": "failed", "exc_info": ValueError(message)},
        )
        assert output == {
            "event": "failed",
            "error_class": "builtins.ValueError",
            "error_message": "request with [redacted:gemini_api_key] failed",
            "frames": [],
        }

    def test_every_exception_field_is_redacted(self) -> None:
        """例外文・型名・frameのファイル名と関数名にも、情報漏洩防止を適用する。"""
        error_type = type(
            "password='synthetic class'", (Exception,), {"__module__": "sample"}
        )

        def fail():
            raise error_type("password='synthetic message'")

        fail.__code__ = fail.__code__.replace(
            co_filename="password='synthetic filename'",
            co_name="password='synthetic function'",
        )
        with pytest.raises(error_type) as captured:
            fail()

        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "error",
            {"event": "failed", "exc_info": captured.value},
        )

        assert output["error_class"] == "sample.password=[redacted:credential]"
        assert output["error_message"] == "password=[redacted:credential]"
        assert output["frames"][-1] == {
            "file": "password=[redacted:credential]",
            "function": "password=[redacted:credential]",
            "line": output["frames"][-1]["line"],
        }

    def test_generated_group_members_are_output_with_secrets_redacted(
        self, configure_chain
    ) -> None:
        """グループから抽出したメンバーは、原因文の秘密情報を伏せて出力する。"""
        capture = configure_chain()
        logger = policy_logger("test", BASE_LOG_RULES)
        group = ExceptionGroup(
            "parallel failures",
            [
                ValueError(f"request with {_GEMINI_KEY} failed"),
                TypeError("invalid response type"),
            ],
        )

        logger.error("failed", exc_info=group)

        entry = capture.entries[0]
        assert entry["exceptions"] == [
            {
                "error_class": "builtins.ValueError",
                "error_message": "request with [redacted:gemini_api_key] failed",
                "frames": [],
            },
            {
                "error_class": "builtins.TypeError",
                "error_message": "invalid response type",
                "frames": [],
            },
        ]
        assert _GEMINI_KEY not in repr(entry)

    def test_normal_and_exception_fields_are_output_with_secrets_redacted(
        self,
    ) -> None:
        """通常項目と例外項目を併記するとき、両方の秘密値を置き換えて出力する。"""
        output = LogPolicyProcessor()(
            PolicyLogger(_TEST_RULES, structlog.ReturnLogger()),
            "error",
            {
                "event": "failed",
                "payload": {"message": "password=normal-secret", "count": 3},
                "exc_info": ValueError("token=exception-secret"),
            },
        )
        assert output == {
            "event": "failed",
            "payload": {"message": "password=[redacted:credential]", "count": 3},
            "error_class": "builtins.ValueError",
            "error_message": "token=[redacted:credential]",
            "frames": [],
            "log_policy": _TEST_RULES.policy.value,
        }

    def test_denied_key_names_are_redacted_before_output(self) -> None:
        """診断に記録した入力由来の禁止キー名にも情報漏洩防止を適用して出力する。"""
        key = "token=synthetic"
        rules = LogPolicyRules(
            LogPolicy.PIPELINE_CONTROL, allow=frozenset(), deny=frozenset({key})
        )
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"event": "completed", key: _SECRET},
        )
        assert output["_denied_keys"] == ["token=[redacted:credential]"]


class TestDiagnostics:
    """除外の診断を安全に出力し、ログごとに独立して保持する。"""

    def test_top_level_and_nested_diagnostics_are_combined(self) -> None:
        """トップレベルとネストで起きた除外を同じログへ集約する。"""
        output = LogPolicyProcessor()(
            PolicyLogger(_TEST_RULES, structlog.ReturnLogger()),
            "info",
            {
                "event": "completed",
                "password": _SECRET,
                "unknown": 1,
                "payload": {"token": _SECRET, "count": 2},
            },
        )
        assert output == {
            "event": "completed",
            "payload": {"count": 2},
            "log_policy": _TEST_RULES.policy.value,
            "_denied_keys": ["password"],
            "_unregistered_count": 1,
            "_denied_nested_count": 1,
        }

    def test_unregistered_key_name_is_not_echoed(self) -> None:
        """未登録の入力キー名そのものを診断フィールドへコピーしない。"""
        key_name = "synthetic private value"
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": "failed", key_name: "ignored"},
        )
        assert output["_unregistered_count"] == 1
        assert key_name not in output
        assert key_name not in repr(output)

    def test_diagnostics_do_not_leak_between_processor_calls(self) -> None:
        """同じprocessorで次のログを処理しても前回の診断は残らない。"""
        processor = LogPolicyProcessor()
        processor(
            PolicyLogger(_TEST_RULES, structlog.ReturnLogger()),
            "info",
            {
                "password": _SECRET,
                "unknown": 1,
                "payload": {"token": _SECRET},
                "x" * 4001: 1,
            },
        )
        assert processor(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": "completed"},
        ) == {"event": "completed"}

    def test_budget_overflow_does_not_affect_next_log(self) -> None:
        """共有予算超過の後も同じprocessorで正常なログを処理できる。"""
        processor = LogPolicyProcessor()
        processor(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {f"unknown_{i}": 1 for i in range(MAX_ITEMS_PER_LOG_EVENT + 1)},
        )
        assert processor(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": "completed"},
        ) == {"event": "completed"}

    def test_diagnostics_are_prepared_outside_value_preparer(self, monkeypatch) -> None:
        """値準備には採用された通常項目だけを渡し、診断は別の入口で準備する。"""
        from app.log_policy.value_preparation import LogValuePreparer

        prepared_inputs = []
        original = LogValuePreparer.prepare_field_value

        def capture_value(self, field_value, **kwargs):
            prepared_inputs.append(field_value)
            return original(self, field_value, **kwargs)

        monkeypatch.setattr(LogValuePreparer, "prepare_field_value", capture_value)
        prepared_event = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": "completed", "password": _SECRET},
        )
        assert prepared_inputs == ["completed"]
        assert prepared_event == {"event": "completed", "_denied_keys": ["password"]}


class TestExceptionFields:
    """例外専用項目への任意入力を除外し、実際の例外から抽出した情報を保護して出力する。"""

    def test_exceptions_argument_is_dropped_even_when_allowed(
        self, configure_chain
    ) -> None:
        """allow宣言があってもログ引数のexceptionsを例外情報として採用しない。"""
        capture = configure_chain()
        rules = BASE_LOG_RULES.extend(allow=frozenset({"exceptions"}))
        logger = policy_logger("test", rules)

        logger.error("failed", exceptions=[{"error_message": "injected failure"}])

        entry = capture.entries[0]
        assert entry["event"] == "failed"
        assert "exceptions" not in entry

    def test_bound_exceptions_are_dropped_even_when_allowed(
        self, configure_chain
    ) -> None:
        """allow宣言があってもbindしたexceptionsを例外情報として採用しない。"""
        capture = configure_chain()
        rules = BASE_LOG_RULES.extend(allow=frozenset({"exceptions"}))
        logger = policy_logger("test", rules).bind(
            exceptions=[{"error_message": "injected failure"}]
        )

        logger.error("failed")

        entry = capture.entries[0]
        assert entry["event"] == "failed"
        assert "exceptions" not in entry

    def test_contextvars_exceptions_are_dropped_even_when_allowed(
        self, configure_chain
    ) -> None:
        """allow宣言があってもcontextvarsのexceptionsを例外情報として採用しない。"""
        capture = configure_chain()
        rules = BASE_LOG_RULES.extend(allow=frozenset({"exceptions"}))
        logger = policy_logger("test", rules)
        structlog.contextvars.bind_contextvars(
            exceptions=[{"error_message": "injected failure"}]
        )

        logger.error("failed")

        entry = capture.entries[0]
        assert entry["event"] == "failed"
        assert "exceptions" not in entry

    def test_generated_exception_message_replaces_same_named_input(self) -> None:
        """同名の入力があっても、例外から抽出した項目を出力に採用する。"""
        output = LogPolicyProcessor()(
            PolicyLogger(_INFRA_RULES, structlog.ReturnLogger()),
            "error",
            {
                "event": "failed",
                "error_message": "handwritten explanation",
                "exc_info": ValueError("token=synthetic"),
            },
        )
        assert output == {
            "event": "failed",
            "error_class": "builtins.ValueError",
            "error_message": "token=[redacted:credential]",
            "frames": [],
            "log_policy": "infrastructure",
        }

    @pytest.mark.parametrize("exc_info", [None, False, "invalid"])
    def test_invalid_exception_info_preserves_input_error_message(
        self, exc_info
    ) -> None:
        """例外項目を生成できない場合は、入力の同名項目を通常どおり準備する。"""
        prepared_event = LogPolicyProcessor()(
            PolicyLogger(_INFRA_RULES, structlog.ReturnLogger()),
            "error",
            {"error_message": "token=synthetic", "exc_info": exc_info},
        )
        assert prepared_event == {
            "error_message": "token=[redacted:credential]",
            "log_policy": "infrastructure",
        }

    def test_invalid_exc_info_is_not_forwarded(self) -> None:
        """processor は不正な exc_info を無視し、生値もキーも出さない。"""
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "error",
            {"event": "failed", "exc_info": (str, "synthetic-row-value", None)},
        )
        assert "exc_info" not in output
        assert "error_class" not in output
        assert "synthetic-row-value" not in json.dumps(output)


def _nested_value(*, depth: int, leaf: Any) -> Any:
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


def _cause_node(fields: Any, cause_depth: int) -> Any:
    for _ in range(cause_depth):
        fields = fields["causes"][0]
    return fields


def _failure_with_cause_chain(cause_depth: int) -> tuple[BaseException, ValueError]:
    try:
        raise ValueError("operation failed")
    except ValueError as exc:
        inner = exc

    outer: BaseException = inner
    for _ in range(cause_depth):
        parent = RuntimeError("operation failed")
        parent.__cause__ = outer
        outer = parent
    return outer, inner


class TestDepthLimit:
    """深さ上限までは通常の規則を適用し、超えた部分だけを置換する。"""

    def test_value_at_depth_limit_is_preserved(self) -> None:
        """深さ上限ちょうどの値は、正常な別項目とともにログへ残す。"""
        rules = BASE_LOG_RULES.extend(allow=frozenset({"payload"}))
        payload = _nested_value(depth=DEPTH_LIMIT, leaf=7)

        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"event": "completed", "payload": payload},
        )

        assert output == {
            "event": "completed",
            "payload": _nested_value(depth=DEPTH_LIMIT, leaf=7),
        }

    def test_denied_field_at_depth_limit_is_removed(self) -> None:
        """深さ上限ちょうどでも、禁止項目を除外して他の項目を残す。"""
        rules = BASE_LOG_RULES.extend(
            allow=frozenset({"payload"}),
            deny=frozenset({"restricted_sample"}),
        )
        payload = _nested_value(
            depth=DEPTH_LIMIT - 1,
            leaf={"restricted_sample": "synthetic", "count": 1},
        )

        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"event": "completed", "payload": payload},
        )

        assert output == {
            "event": "completed",
            "payload": _nested_value(
                depth=DEPTH_LIMIT - 1,
                leaf={"count": 1},
            ),
            "_denied_nested_count": 1,
        }

    def test_registered_field_at_depth_limit_is_sanitized(self) -> None:
        """深さ上限ちょうどの登録項目にも、サニタイズを適用する。"""
        rules = BASE_LOG_RULES.extend(
            allow=frozenset({"payload"}),
            sanitize=frozenset({"canonical_url"}),
        )
        payload = _nested_value(
            depth=DEPTH_LIMIT - 1,
            leaf={"canonical_url": "https://example.com/a/1?p=123#top"},
        )

        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"event": "completed", "payload": payload},
        )

        assert output == {
            "event": "completed",
            "payload": _nested_value(
                depth=DEPTH_LIMIT - 1,
                leaf={"canonical_url": "https://example.com/a/1?p=123"},
            ),
        }

    def test_text_at_depth_limit_is_redacted(self) -> None:
        """深さ上限ちょうどの文字列にも、情報漏洩防止を適用する。"""
        rules = BASE_LOG_RULES.extend(allow=frozenset({"payload"}))
        payload = _nested_value(
            depth=DEPTH_LIMIT - 1,
            leaf={"message": "password=synthetic"},
        )

        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"event": "completed", "payload": payload},
        )

        assert output == {
            "event": "completed",
            "payload": _nested_value(
                depth=DEPTH_LIMIT - 1,
                leaf={"message": "password=[redacted:credential]"},
            ),
        }

    def test_dictionary_value_beyond_depth_limit_is_replaced(self) -> None:
        """辞書内の値が深さ上限を超えたら、項目名を残して値を [limit] に置き換える。"""
        rules = BASE_LOG_RULES.extend(allow=frozenset({"payload"}))
        payload = _nested_value(depth=DEPTH_LIMIT, leaf={"value": 7})

        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"event": "completed", "payload": payload},
        )

        assert output == {
            "event": "completed",
            "payload": _nested_value(depth=DEPTH_LIMIT, leaf={"value": "[limit]"}),
        }

    def test_list_value_beyond_depth_limit_is_replaced(self) -> None:
        """配列内の値が深さ上限を超えたら、その要素を [limit] に置き換える。"""
        rules = BASE_LOG_RULES.extend(allow=frozenset({"payload"}))
        payload = _nested_value(depth=DEPTH_LIMIT, leaf=["synthetic"])

        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"event": "completed", "payload": payload},
        )

        assert output == {
            "event": "completed",
            "payload": _nested_value(depth=DEPTH_LIMIT, leaf=["[limit]"]),
        }

    def test_dictionary_inside_list_beyond_depth_limit_is_replaced(self) -> None:
        """配列内の辞書が深さ上限を超えたら、辞書全体を [limit] に置き換える。"""
        rules = BASE_LOG_RULES.extend(allow=frozenset({"payload"}))
        payload = _nested_value(depth=DEPTH_LIMIT, leaf=[{"value": 7}])

        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"event": "completed", "payload": payload},
        )

        assert output == {
            "event": "completed",
            "payload": _nested_value(depth=DEPTH_LIMIT, leaf=["[limit]"]),
        }

    def test_list_inside_dictionary_beyond_depth_limit_is_replaced(self) -> None:
        """辞書内の配列が深さ上限を超えたら、配列全体を [limit] に置き換える。"""
        rules = BASE_LOG_RULES.extend(allow=frozenset({"payload"}))
        payload = _nested_value(depth=DEPTH_LIMIT, leaf={"items": [7]})

        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"event": "completed", "payload": payload},
        )

        assert output == {
            "event": "completed",
            "payload": _nested_value(depth=DEPTH_LIMIT, leaf={"items": "[limit]"}),
        }

    def test_depth_over_limit_preserves_and_redacts_siblings(self) -> None:
        """深さ上限を超えた部分だけを置換し、隣の正常な文字列には情報漏洩防止を適用する。"""
        nested = _nested_value(depth=DEPTH_LIMIT + 1, leaf="synthetic")

        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": {"first": "token=synthetic", "nested": nested}},
        )

        assert output == {
            "event": {
                "first": "token=[redacted:credential]",
                "nested": _nested_value(depth=DEPTH_LIMIT, leaf="[limit]"),
            },
        }


class TestExceptionValueDepthLimit:
    """通常入力と例外データで、値準備の深さ上限を使い分ける。"""

    @pytest.fixture
    def exception_at_value_limit(self, monkeypatch: pytest.MonkeyPatch):
        """例外用の値準備上限内にframeの値が収まる、最も深い連鎖を用意する。"""
        # 原因1段で2階層が増え、frameの値はさらに2階層深くなる。
        cause_depth = (value_preparation.EXCEPTION_DEPTH_LIMIT - 2) // 2
        outer, inner = _failure_with_cause_chain(cause_depth)
        # 探索側で先に打ち切られないようにし、値準備の上限は変更しない。
        monkeypatch.setattr(extraction, "CAUSE_DEPTH_LIMIT", cause_depth)
        monkeypatch.setattr(extraction, "EXCEPTION_LIMIT", cause_depth + 1)
        tb = inner.__traceback__
        expected_frame = {
            "file": tb.tb_frame.f_code.co_filename,
            "function": tb.tb_frame.f_code.co_name,
            "line": tb.tb_lineno,
        }
        return outer, cause_depth, expected_frame

    @pytest.fixture
    def exception_beyond_value_limit(self, monkeypatch: pytest.MonkeyPatch):
        """原因文とframe辞書を上限内に置き、frameの各値を上限の外に置く。"""
        cause_depth = value_preparation.EXCEPTION_DEPTH_LIMIT // 2
        outer, _ = _failure_with_cause_chain(cause_depth)
        # 探索側で先に打ち切られないようにし、値準備の上限は変更しない。
        monkeypatch.setattr(extraction, "CAUSE_DEPTH_LIMIT", cause_depth)
        monkeypatch.setattr(extraction, "EXCEPTION_LIMIT", cause_depth + 1)
        return outer, cause_depth

    def test_normal_and_exception_values_use_their_own_limits(
        self, exception_at_value_limit
    ) -> None:
        """通常入力は通常上限で止まり、例外のframe情報は例外用の上限内で残る。"""
        exc, cause_depth, expected_frame = exception_at_value_limit
        normal_value = _nested_value(
            depth=value_preparation.EXCEPTION_DEPTH_LIMIT, leaf="normal value"
        )
        rules = BASE_LOG_RULES.extend(allow=frozenset({"payload"}))

        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "error",
            {"event": "failed", "payload": normal_value, "exc_info": exc},
        )

        assert output["payload"] == _nested_value(
            depth=value_preparation.DEPTH_LIMIT + 1, leaf="[limit]"
        )
        inner = _cause_node(output, cause_depth)
        assert inner["frames"] == [expected_frame]

    def test_exception_value_beyond_its_limit_is_replaced(
        self, exception_beyond_value_limit
    ) -> None:
        """上限内の原因文を残し、上限直後のframeの各値だけをlimitに置き換える。"""
        exc, cause_depth = exception_beyond_value_limit

        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "error",
            {"event": "failed", "exc_info": exc},
        )

        inner = _cause_node(output, cause_depth)
        assert inner["error_message"] == "operation failed"
        assert inner["frames"] == [
            {"file": "[limit]", "function": "[limit]", "line": "[limit]"}
        ]


class TestProcessingOrder:
    """トップレベルの選別を終えてから採用した値を準備し、不採用の値は準備処理へ渡さない。"""

    def test_all_fields_are_selected_before_values_are_prepared(
        self, monkeypatch
    ) -> None:
        """トップレベルの項目をすべて選別してから、受け付けた項目の値を準備する。"""
        from app.log_policy.value_preparation import LogValuePreparer

        steps = []
        original = LogValuePreparer.prepare_field_value

        class EventFields(dict):
            def items(self):
                steps.append("read_payload")
                yield "payload", {"message": "token=synthetic"}
                steps.append("read_event")
                yield "event", "completed"

        def prepare_value(self, field_value, **kwargs):
            prepared_value = original(self, field_value, **kwargs)
            steps.append(prepared_value)
            return prepared_value

        monkeypatch.setattr(LogValuePreparer, "prepare_field_value", prepare_value)
        LogPolicyProcessor()(
            PolicyLogger(_TEST_RULES, structlog.ReturnLogger()), "info", EventFields()
        )
        assert steps == [
            "read_payload",
            "read_event",
            {"message": "token=[redacted:credential]"},
            "completed",
        ]

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({"password": object()}, {"_denied_keys": ["password"]}),
            ({"unknown": object()}, {"_unregistered_count": 1}),
            ({"x" * (TEXT_LIMIT + 1): object()}, {"_policy_limited": True}),
        ],
        ids=["denied", "unregistered", "long_name"],
    )
    def test_excluded_field_value_is_not_prepared(
        self, monkeypatch, fields: dict, expected: dict
    ) -> None:
        """禁止・未登録・長すぎる名前の項目の値は構造検査やサニタイズへ渡さず除外する。"""
        from app.log_policy.value_preparation import LogValuePreparer

        def unexpected_preparation(self, field_value, **kwargs):
            raise AssertionError("excluded value must not be prepared")

        monkeypatch.setattr(
            LogValuePreparer, "prepare_field_value", unexpected_preparation
        )
        prepared_event = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()), "info", fields
        )
        assert prepared_event == expected

    def test_top_level_budget_overflow_stops_input_iteration(self) -> None:
        """上限超過を確認した一件より後の入力は取り出さない。"""

        class BoundedInput(dict):
            def items(self):
                for i in range(MAX_ITEMS_PER_LOG_EVENT + 1):
                    yield f"unknown_{i}", 1
                raise AssertionError("must not read after budget overflow")

        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            BoundedInput(),
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "value_count",
        }

    def test_nested_budget_overflow_skips_later_field_values(self, monkeypatch) -> None:
        """値の走査で予算を超えたら、後続項目の値は準備しない。"""
        from app.log_policy.value_preparation import LogValuePreparer

        prepared_field_names = []
        original = LogValuePreparer.prepare_field_value

        def prepare_value(self, field_value, **kwargs):
            prepared_field_names.append(kwargs["field_name"])
            return original(self, field_value, **kwargs)

        monkeypatch.setattr(LogValuePreparer, "prepare_field_value", prepare_value)
        prepared_event = LogPolicyProcessor()(
            PolicyLogger(_TEST_RULES, structlog.ReturnLogger()),
            "info",
            {"payload": [1] * MAX_ITEMS_PER_LOG_EVENT, "event": "completed"},
        )
        assert prepared_field_names == ["payload"]
        assert prepared_event == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "value_count",
        }


class TestProcessingFailures:
    """処理に失敗した場合は原文を出力せず固定エラーを返す。"""

    def test_processor_without_policy_logger_returns_fixed_error(self) -> None:
        """ルールを持つロガーの接続が欠けても原文を返さず固定エラーにする。"""
        assert LogPolicyProcessor()(
            structlog.ReturnLogger(), "info", {"event": _SECRET}
        ) == {"event": "log_policy_failed", "_policy_error": "processing_failed"}

    def test_diagnostic_leak_prevention_failure_does_not_restore_raw_key_names(
        self,
        monkeypatch,
    ) -> None:
        """診断の情報漏洩防止が失敗しても原文に戻らず固定エラーだけを返す。"""

        def fail(text):
            raise ValueError("synthetic-private-key")

        monkeypatch.setattr("app.log_policy.diagnostics.prevent_credential_leaks", fail)
        prepared_event = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": "completed", "password": _SECRET},
        )
        assert prepared_event == {
            "event": "log_policy_failed",
            "_policy_error": "processing_failed",
        }

    def test_value_leak_prevention_failure_returns_only_fixed_metadata(
        self, monkeypatch
    ) -> None:
        """値の情報漏洩防止が失敗しても業務側へ例外や原文を返さない。"""

        def fail(*_, **__):
            raise ValueError(_SECRET)

        monkeypatch.setattr(
            "app.log_policy.value_preparation.prevent_credential_leaks", fail
        )
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "error",
            {"event": _SECRET},
        )
        assert output == {
            "event": "log_policy_failed",
            "_policy_error": "processing_failed",
        }

    def test_processing_failure_emits_one_fixed_event_without_reentry(
        self, monkeypatch, configure_chain
    ) -> None:
        """保護処理の失敗を保護処理自身で記録せず、固定イベント1件だけを出す。"""

        def fail(*_, **__):
            raise ValueError(_SECRET)

        monkeypatch.setattr(
            "app.log_policy.value_preparation.prevent_credential_leaks", fail
        )
        processed_methods = []
        original = LogPolicyProcessor.__call__

        def counted(self, logger, method_name, event_dict):
            processed_methods.append(method_name)
            return original(self, logger, method_name, event_dict)

        monkeypatch.setattr(LogPolicyProcessor, "__call__", counted)
        capture = configure_chain()
        policy_logger("test", BASE_LOG_RULES).error(_SECRET)
        assert [dict(entry) for entry in capture.entries] == [
            {
                "event": "log_policy_failed",
                "_policy_error": "processing_failed",
                "log_level": "error",
            }
        ]
        assert processed_methods == ["error"]
