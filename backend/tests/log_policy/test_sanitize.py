"""文字列秘匿の契約: 識別できる認証情報の形式を伏せ、通常文と識別情報は残す。"""

from __future__ import annotations

import pytest

from app.log_policy.sanitize import TEXT_LIMIT, sanitize_text

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("token=x", "token=***", id="short_token"),
        pytest.param("x-api-key: x", "x-api-key: ***", id="short_api_key"),
        pytest.param(
            "env PGPASSWORD=hunter2redacted psql failed",
            "env PGPASSWORD=*** psql failed",
            id="env_password",
        ),
        pytest.param(
            "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY region=x",
            "aws_secret_access_key=*** region=x",
            id="aws_secret",
        ),
        pytest.param(
            "?DBUser=app&X-Amz-Signature=0123456789abcdef0123456789abcdef",
            "?DBUser=app&X-Amz-Signature=***",
            id="sigv4_query",
        ),
    ],
)
def test_unquoted_assignment_replaces_the_whole_token(text: str, expected: str) -> None:
    """引用符なしのキー付き値は単一トークン全体を伏せ、周囲の文は残す。"""
    assert sanitize_text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "password='synthetic private value'",
            "password=***",
            id="spaces",
        ),
        pytest.param(
            'password="synthetic \\"private\\" value"',
            "password=***",
            id="escaped_quotes",
        ),
        pytest.param(
            "password='synthetic\nprivate value'",
            "password=***",
            id="newline",
        ),
        pytest.param(
            "{'user': 'vector', 'password': 'hunter2redacted', 'host': 'db'}",
            "{'user': 'vector', 'password': ***, 'host': 'db'}",
            id="dict_repr",
        ),
        pytest.param(
            "headers={'x-api-key': 'super-secret-value-here-1234'}",
            "headers={'x-api-key': ***}",
            id="quoted_api_key",
        ),
        pytest.param(
            "headers={'Authorization': 'Bearer "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.abc'}",
            "headers={'Authorization': ***}",
            id="quoted_authorization",
        ),
        pytest.param(
            "gemini_api_key='synthetic secret' failed",
            "gemini_api_key=*** failed",
            id="application_key_name",
        ),
    ],
)
def test_quoted_assignment_replaces_the_whole_value(text: str, expected: str) -> None:
    """引用符内は空白・改行・エスケープを含めて値全体を伏せ、閉じ引用符の外は残す。"""
    assert sanitize_text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "password='synthetic private value",
            "password=***",
            id="unterminated",
        ),
        pytest.param(
            "password='synthetic private value\\",
            "password=***",
            id="trailing_escape",
        ),
    ],
)
def test_unterminated_quoted_value_is_redacted_to_the_end(
    text: str, expected: str
) -> None:
    """閉じていない引用符は末尾まで伏せ、バックスラッシュで終わっても断片を残さない。"""
    assert sanitize_text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "Authorization: Basic dTpw",
            "Authorization: ***",
            id="basic_auth",
        ),
        pytest.param(
            "Cookie: session=synthetic; second=private",
            "Cookie: ***",
            id="cookie_pairs",
        ),
    ],
)
def test_header_value_is_redacted_to_the_end_of_line(text: str, expected: str) -> None:
    """認証ヘッダーと cookie は行末までの値全体を伏せる。"""
    assert sanitize_text(text) == expected


def test_private_key_block_is_removed_as_a_whole() -> None:
    """PEM 秘密鍵は BEGIN から END までを本文ごと伏せ、前後の文は残す。"""
    secret = "synthetic private value"
    text = f"failed -----BEGIN PRIVATE KEY-----\n{secret}\n-----END PRIVATE KEY-----"
    assert sanitize_text(text) == "failed ***"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "Error from AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q",
            "Error from AIza***",
            id="gemini",
        ),
        pytest.param(
            "key=sk-ant-api03-abcdef0123456789ABCDEFxyz_-X failed",
            "key=sk-ant-*** failed",
            id="anthropic",
        ),
        pytest.param(
            "sk-proj-abcdef0123456789ABCDEFxyz used",
            "sk-*** used",
            id="openai",
        ),
        pytest.param(
            "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890 on /repos",
            "gh*_*** on /repos",
            id="github",
        ),
        pytest.param(
            "tvly-abcdefghijklmnopqrstuvwxyz0123 with 401",
            "tvly-*** with 401",
            id="tavily",
        ),
        pytest.param(
            "for AKIAIOSFODNN7EXAMPLE in request",
            "for AKIA*** in request",
            id="aws_access_key",
        ),
        pytest.param(
            "postgresql+asyncpg://vector:s3cr3tp4ss@db:5432/vector",
            "postgresql+asyncpg://***@db:5432/vector",
            id="dsn_userinfo",
        ),
        pytest.param(
            "rediss://default:hunter2redacted@cache:6380/0",
            "rediss://***@cache:6380/0",
            id="redis_userinfo",
        ),
        pytest.param(
            "got eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4 upstream",
            "got eyJ*** upstream",
            id="jwt",
        ),
    ],
)
def test_known_credential_form_is_replaced_and_context_kept(
    text: str, expected: str
) -> None:
    """キー無しの既知形式は値全体を置換し、周囲の原因文と host は残す。"""
    assert sanitize_text(text) == expected


@pytest.mark.parametrize(
    "sample",
    [
        "Connection refused on host db.internal port 5432",
        "https://www.anthropic.com/news/claude-4-release",
        "postgres://db:5432/vector",
        "GET /repos/owner/Authorization-utils/contents/README returned 200",
        "arn:aws:ssm:ap-northeast-1:123456789012:parameter/vector/db",
        "vector-db.cluster-abc.ap-northeast-1.rds.amazonaws.com:5432",
        "rds_iam_auth_token_port=5432 hide_parameters=True",
        "completion_tokens=128 max_tokens=1024",
        "取得に失敗しました: timeout after 30s",
    ],
)
def test_normal_text_is_preserved_unchanged(sample: str) -> None:
    """通常テキストと識別情報は無変化のまま残す。"""
    assert sanitize_text(sample) == sample


def test_sanitizer_preserves_length_until_output_protection() -> None:
    """サニタイズは置換のみを担い、文字数制限を暗黙適用しない。"""
    text = "y" * (TEXT_LIMIT * 3)
    assert sanitize_text(text) == text
