"""文字列中の認証キー名を目印に、キーより後ろを種別付きの表記へ置き換える。"""

from __future__ import annotations

import pytest

from app.log_policy.leak_prevention import redact_credential_assignments

pytestmark = pytest.mark.unit

# 合成値を分割し、秘密検出ツールの規則に一致させない。
_GEMINI_KEY = "AIza" + "SyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"


class TestKeyMatching:
    """認証キー名は正規化後の完全一致で照合し、部分一致や値の内容だけでは置き換えない。"""

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param(
                "GET /repos/owner/Authorization-utils/contents/README returned 200",
                id="authorization_word_in_path",
            ),
            pytest.param(
                "rds_iam_auth_token_port=5432 hide_parameters=True",
                id="iam_token_port_setting",
            ),
            pytest.param(
                "completion_tokens=128 max_tokens=1024", id="token_usage_metrics"
            ),
            pytest.param("clientToken=synthetic failed", id="client_token"),
            pytest.param("password_hash=synthetic failed", id="password_hash"),
            pytest.param("my_password=synthetic failed", id="my_password"),
        ],
    )
    def test_key_containing_credential_word_is_preserved(self, text: str) -> None:
        """認証キー名を部分文字列として含むだけの別のキーや語を置き換えない。"""
        assert redact_credential_assignments(text) == text

    @pytest.mark.parametrize("key", ["ApiKey", "api-key", "API_KEY"])
    def test_credential_key_is_matched_after_normalization(self, key: str) -> None:
        """表記揺れのある認証キー名も正規化後の完全一致で照合する。"""
        assert redact_credential_assignments(f"{key}=synthetic") == (
            f"{key}=[redacted:credential]"
        )

    def test_value_of_non_credential_key_is_not_inspected(self) -> None:
        """認証キー名でない項目の値は、既知の認証情報の形式でもこの処理では置き換えない。"""
        text = f"other={_GEMINI_KEY}"
        assert redact_credential_assignments(text) == text


class TestRedactionRange:
    """キー名と区切りを残し、その後ろは値の終わりを推測せず末尾まで置き換える。"""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("token=x", "token=[redacted:credential]", id="equals"),
            pytest.param(
                "x-api-key: x", "x-api-key: [redacted:credential]", id="colon"
            ),
            pytest.param(
                "Authorization: Bearer synthetic",
                "Authorization: [redacted:credential]",
                id="authorization_header",
            ),
            pytest.param(
                "Cookie: session=synthetic; second=private",
                "Cookie: [redacted:credential]",
                id="cookie_header",
            ),
        ],
    )
    def test_credential_value_is_replaced_with_key_and_separator_kept(
        self, text: str, expected: str
    ) -> None:
        """キー名と区切り文字を残し、値を種別付きの表記に置き換える。"""
        assert redact_credential_assignments(text) == expected

    def test_text_before_key_is_preserved(self) -> None:
        """キーより前の原因文は残す。"""
        text = "env PGPASSWORD=hunter2redacted psql failed"
        assert redact_credential_assignments(text) == (
            "env PGPASSWORD=[redacted:credential]"
        )

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("abc,def host=db", id="comma"),
            pytest.param("abc&def host=db", id="ampersand"),
            pytest.param("abc;def host=db", id="semicolon"),
            pytest.param("abc def host=db", id="space"),
            pytest.param("abc}def host=db", id="closing_brace"),
            pytest.param("'abc\\'def' host=db", id="escaped_quote"),
            pytest.param("'abc\ndef'\nstatus=failed", id="newline"),
            pytest.param("['abc', 'def'] host=db", id="container"),
            pytest.param("'unterminated abc def", id="unterminated_quote"),
        ],
    )
    def test_value_is_replaced_to_the_end_regardless_of_delimiters(
        self, value: str
    ) -> None:
        """値に区切り文字や改行が含まれても、推測した終端の後ろに断片を残さない。"""
        assert redact_credential_assignments(f"password={value}") == (
            "password=[redacted:credential]"
        )

    def test_dict_repr_is_replaced_from_credential_key_to_the_end(self) -> None:
        """辞書表現では認証キーより前の項目を残し、認証キーより後ろを置き換える。"""
        text = "{'user': 'vector', 'password': 'hunter2redacted', 'host': 'db'}"
        assert redact_credential_assignments(text) == (
            "{'user': 'vector', 'password': [redacted:credential]"
        )

    def test_first_credential_key_ends_the_output(self) -> None:
        """最初の認証キーより後ろは、別の認証キーを含めて一度の置換で覆う。"""
        text = "token=first password=second"
        assert redact_credential_assignments(text) == "token=[redacted:credential]"

    @pytest.mark.parametrize(
        "key",
        [
            "gemini_api_key",
            "deepseek_api_key",
            "logfire_token",
            "aws_secret_access_key",
            "aws_session_token",
            "private_key",
        ],
    )
    def test_application_credential_keys_are_recognized(self, key: str) -> None:
        """アプリの設定名や認証情報のキー名も目印として扱う。"""
        assert redact_credential_assignments(f"{key}='synthetic' region=x") == (
            f"{key}=[redacted:credential]"
        )

    @pytest.mark.parametrize("text", ["password=", "password: "])
    def test_key_without_value_is_preserved(self, text: str) -> None:
        """区切りの後ろに値がなければ置き換える対象がないため、そのまま残す。"""
        assert redact_credential_assignments(text) == text
