"""ログ一件の診断の記録・集計・状態分離を検証する。"""

import pytest

from app.log_policy.budget import (
    EVENT_TEXT_LIMIT,
    MAX_ITEMS_PER_LOG_EVENT,
    TEXT_LIMIT,
    LogBudgetExceeded,
    LogEventBudget,
)
from app.log_policy.diagnostics import LogProcessingDiagnostics

pytestmark = pytest.mark.unit


class TestRecording:
    """記録は名前を残すものと件数だけのものを区別し、順序と重複を保つ。"""

    def test_empty_diagnostics_has_no_fields(self) -> None:
        """出来事がなければ診断項目を出さない。"""
        assert LogProcessingDiagnostics().as_fields() == {}

    def test_top_level_denied_records_key_only(self) -> None:
        """トップレベルの禁止項目はキー名だけ記録する。"""
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_top_level_denied("password")
        assert diagnostics.as_fields() == {"_denied_keys": ["password"]}

    def test_unregistered_records_count_only(self) -> None:
        """未登録項目は入力名を受け取らず件数だけ増やす。"""
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_unregistered()
        diagnostics.record_unregistered()
        assert diagnostics.as_fields() == {"_unregistered_count": 2}

    def test_top_level_limit_records_flag(self) -> None:
        """上限到達が複数回あっても既存の真偽値で表す。"""
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_top_level_limit_reached()
        diagnostics.record_top_level_limit_reached()
        assert diagnostics.as_fields() == {"_policy_limited": True}

    def test_nested_denied_records_count_only(self) -> None:
        """ネストの禁止項目は名前を保存せず件数だけ増やす。"""
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_nested_denied()
        assert diagnostics.as_fields() == {"_denied_nested_count": 1}

    def test_nested_invalid_key_records_exclusion_count(self) -> None:
        """ネストの非文字列キーは既存の除外件数へ数える。"""
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_nested_invalid_key()
        assert diagnostics.as_fields() == {"_denied_nested_count": 1}

    def test_nested_exclusion_reasons_share_existing_count(self) -> None:
        """禁止キーと非文字列キーの除外は同じ既存項目へ合算する。"""
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_nested_denied()
        diagnostics.record_nested_invalid_key()
        assert diagnostics.as_fields() == {"_denied_nested_count": 2}

    def test_denied_keys_preserve_order_and_duplicates(self) -> None:
        """禁止キーの記録順と重複をそのまま残す。"""
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_top_level_denied("token")
        diagnostics.record_top_level_denied("password")
        diagnostics.record_top_level_denied("token")
        assert diagnostics.as_fields() == {
            "_denied_keys": ["token", "password", "token"]
        }

    def test_nested_long_key_records_limit_diagnostic(self) -> None:
        """ネストの長すぎるキーの除外は既存の上限診断へ記録する。"""
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_nested_key_limit_reached()
        assert diagnostics.as_fields() == {"_policy_limited": True}


class TestStateIsolation:
    """返した辞書や一覧を変更しても内部状態は変わらない。"""

    def test_returned_mapping_does_not_expose_internal_state(self) -> None:
        """返した辞書を変更しても診断の内部状態は変わらない。"""
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_unregistered()
        fields = diagnostics.as_fields()
        fields.clear()
        assert diagnostics.as_fields() == {"_unregistered_count": 1}

    def test_returned_denied_keys_do_not_expose_internal_list(self) -> None:
        """返した禁止キー一覧を変更しても内部の一覧は変わらない。"""
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_top_level_denied("password")
        fields = diagnostics.as_fields()
        fields["_denied_keys"].append("token")
        assert diagnostics.as_fields() == {"_denied_keys": ["password"]}


class TestPreparedFields:
    """情報漏洩防止を適用して共有予算に計上し、超過時は情報漏洩防止の前に中断する。"""

    def test_prepared_denied_keys_preserve_order_and_duplicates(self) -> None:
        """情報漏洩防止の後もキー名の記録順と重複を保持する。"""
        diagnostics = LogProcessingDiagnostics()
        for key in ["token=first", "password=second", "token=first"]:
            diagnostics.record_top_level_denied(key)
        assert diagnostics.prepare_log_fields(budget=LogEventBudget()) == {
            "_denied_keys": [
                "token=[redacted:credential]",
                "password=[redacted:credential]",
                "token=[redacted:credential]",
            ]
        }

    def test_prepared_denied_keys_do_not_mutate_recorded_keys(self) -> None:
        """準備や返却値の変更で記録済みのキー名を変更しない。"""
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_top_level_denied("token=synthetic")
        fields = diagnostics.prepare_log_fields(budget=LogEventBudget())
        fields["_denied_keys"].clear()
        assert diagnostics.as_fields() == {"_denied_keys": ["token=synthetic"]}

    def test_denied_keys_consume_shared_items_and_original_text(self) -> None:
        """診断のフィールド・要素と置換前の文字数を既存予算へ加算する。"""
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_top_level_denied("token=synthetic")
        budget = LogEventBudget(log_item_count=10, counted_text_chars=100)
        diagnostics.prepare_log_fields(budget=budget)
        assert budget.log_item_count == 12
        assert budget.counted_text_chars == 100 + len("_denied_keys") + len(
            "token=synthetic"
        )

    def test_fixed_diagnostics_do_not_consume_shared_budget(self) -> None:
        """固定の件数・フラグには入力由来の文字列用予算を使わない。"""
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_unregistered()
        diagnostics.record_nested_denied()
        diagnostics.record_top_level_limit_reached()
        budget = LogEventBudget(
            log_item_count=MAX_ITEMS_PER_LOG_EVENT, counted_text_chars=EVENT_TEXT_LIMIT
        )
        assert diagnostics.prepare_log_fields(budget=budget) == {
            "_unregistered_count": 1,
            "_denied_nested_count": 1,
            "_policy_limited": True,
        }
        assert budget.log_item_count == MAX_ITEMS_PER_LOG_EVENT
        assert budget.counted_text_chars == EVENT_TEXT_LIMIT

    def test_denied_key_at_text_limit_is_redacted_once(self, monkeypatch) -> None:
        """単一上限ちょうどのキー名に一度だけ情報漏洩防止を適用する。"""
        from unittest.mock import Mock

        redact = Mock(return_value="redacted")
        monkeypatch.setattr(
            "app.log_policy.diagnostics.prevent_credential_leaks", redact
        )
        diagnostics = LogProcessingDiagnostics()
        key = "x" * TEXT_LIMIT
        diagnostics.record_top_level_denied(key)
        assert diagnostics.prepare_log_fields(budget=LogEventBudget()) == {
            "_denied_keys": ["redacted"]
        }
        redact.assert_called_once_with(key)

    def test_denied_key_above_text_limit_is_replaced_without_leak_prevention(
        self,
        monkeypatch,
    ) -> None:
        """長すぎる診断文字列は原文の文字数計上も情報漏洩防止もせず置換する。"""
        from unittest.mock import Mock

        redact = Mock()
        monkeypatch.setattr(
            "app.log_policy.diagnostics.prevent_credential_leaks", redact
        )
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_top_level_denied("x" * (TEXT_LIMIT + 1))
        budget = LogEventBudget()
        assert diagnostics.prepare_log_fields(budget=budget) == {
            "_denied_keys": ["[limit]"]
        }
        assert budget.counted_text_chars == len("_denied_keys")
        redact.assert_not_called()

    def test_diagnostic_item_overflow_stops_before_redacting_any_key(
        self, monkeypatch
    ) -> None:
        """一覧の後続要素で件数を超過したときも先行キーに情報漏洩防止を適用しない。"""
        from unittest.mock import Mock

        redact = Mock()
        monkeypatch.setattr(
            "app.log_policy.diagnostics.prevent_credential_leaks", redact
        )
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_top_level_denied("token=first")
        diagnostics.record_top_level_denied("password=second")
        budget = LogEventBudget(log_item_count=MAX_ITEMS_PER_LOG_EVENT - 2)
        with pytest.raises(LogBudgetExceeded, match="value_count"):
            diagnostics.prepare_log_fields(budget=budget)
        redact.assert_not_called()

    def test_diagnostic_text_overflow_stops_before_redacting_any_key(
        self, monkeypatch
    ) -> None:
        """診断の文字数も既存予算と合算し、超過時は情報漏洩防止の前に中断する。"""
        from unittest.mock import Mock

        redact = Mock()
        monkeypatch.setattr(
            "app.log_policy.diagnostics.prevent_credential_leaks", redact
        )
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_top_level_denied("token=first")
        diagnostics.record_top_level_denied("password=second")
        budget = LogEventBudget(
            counted_text_chars=EVENT_TEXT_LIMIT - len("_denied_keystoken=first")
        )
        with pytest.raises(LogBudgetExceeded, match="text_total"):
            diagnostics.prepare_log_fields(budget=budget)
        redact.assert_not_called()
