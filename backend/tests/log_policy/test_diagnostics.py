"""ログ一件の診断の記録・集計・状態分離を検証する。"""

import pytest

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

    def test_nested_denied_records_count_only(self) -> None:
        """ネストの禁止項目は名前を保存せず件数だけ増やす。"""
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_nested_denied()
        assert diagnostics.as_fields() == {"_denied_nested_count": 1}

    def test_denied_keys_preserve_order_and_duplicates(self) -> None:
        """禁止キーの記録順と重複をそのまま残す。"""
        diagnostics = LogProcessingDiagnostics()
        diagnostics.record_top_level_denied("token")
        diagnostics.record_top_level_denied("password")
        diagnostics.record_top_level_denied("token")
        assert diagnostics.as_fields() == {
            "_denied_keys": ["token", "password", "token"]
        }


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
