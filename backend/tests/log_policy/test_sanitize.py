"""項目名と値から対応する処理を選ぶ、サニタイズ共通入口の契約。"""

from __future__ import annotations

import pytest

from app.log_policy import sanitize

pytestmark = pytest.mark.unit


class TestFieldSanitization:
    def test_field_with_wrong_type_returns_unsupported(self) -> None:
        """文字列を期待するconnection_urlに数値を渡すと、[unsupported]を返す。"""
        result = sanitize.sanitize_field_value("connection_url", 123)

        assert result == "[unsupported]"

    def test_connection_url_returns_url_sanitization_result(self) -> None:
        """connection_urlに文字列を渡すと、対応するURLサニタイズで処理した値を返す。"""
        result = sanitize.sanitize_field_value(
            "connection_url",
            "https://user:synthetic@example.com/path?note=eyJabc.eyJdef.signature",
        )

        assert result == "https://***@example.com/path?note=eyJabc.eyJdef.signature"

    def test_upstream_message_returns_jwt_sanitization_result(self) -> None:
        """upstream_messageに文字列を渡すと、対応するJWTサニタイズで処理した値を返す。"""
        result = sanitize.sanitize_field_value(
            "upstream_message",
            "got eyJabc.eyJdef.signature from https://user:synthetic@example.com/path",
        )

        assert result == ("got eyJ*** from https://user:synthetic@example.com/path")
