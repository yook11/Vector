"""``app.shared.security.redaction.redact_secrets`` のテスト。

検証する性質:
1. **網羅性 (must-redact)**: 既知 secret pattern が任意の位置・文脈で `***`
   に置換されること
2. **可読性 (must-preserve)**: 通常 error / stack trace / URL / 日本語等が
   過剰 redact されないこと
3. **anti-test**: redact 後の output に既知 secret literal が一切残らないこと
   (実装と test の癒着回避のため別 detector で再走査)
4. **境界条件**: 空文字 / 超長文字列 / multibyte が壊れないこと
"""

from __future__ import annotations

import re

import pytest

from app.shared.security.redaction import redact_secrets

# ---------------------------------------------------------------------------
# A. 網羅性テスト (must-redact)
# ---------------------------------------------------------------------------


def test_redacts_google_aiza_key() -> None:
    text = "Error from AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q not authorized"
    redacted = redact_secrets(text)
    assert "AIzaSyA1B2C3" not in redacted
    assert "AIza***" in redacted


def test_redacts_anthropic_key() -> None:
    # 実 Anthropic key 形式を模した synthetic 値 (本物ではない)
    text = "key=sk-ant-api03-abcdef0123456789ABCDEF0123456789xyz_-XXX failed"
    redacted = redact_secrets(text)
    assert "abcdef0123456789" not in redacted
    assert "sk-ant-***" in redacted


def test_redacts_openai_legacy_key() -> None:
    text = "OpenAI sk-abcdef0123456789ABCDEFxyz returned 401"
    redacted = redact_secrets(text)
    assert "abcdef0123456789" not in redacted
    assert "sk-***" in redacted


def test_redacts_openai_project_key() -> None:
    text = "OpenAI project key sk-proj-abcdef0123456789ABCDEFxyz used"
    redacted = redact_secrets(text)
    assert "abcdef0123456789" not in redacted
    assert "sk-***" in redacted


def test_redacts_openai_service_account_key() -> None:
    text = "service account sk-svcacct-abcdef0123456789xyz failed"
    redacted = redact_secrets(text)
    assert "abcdef0123456789" not in redacted
    assert "sk-***" in redacted


@pytest.mark.parametrize(
    "prefix",
    ["ghp_", "gho_", "ghu_", "ghs_", "ghr_"],
)
def test_redacts_github_pat_class(prefix: str) -> None:
    token = prefix + "aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890"  # 36 char body
    text = f"GitHub call returned 401 for {token} on /repos"
    redacted = redact_secrets(text)
    assert "aBcDeFgHiJkLmNoP" not in redacted
    assert "gh*_***" in redacted


def test_redacts_authorization_bearer_header() -> None:
    text = (
        "headers={'Authorization': 'Bearer "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4eHgifQ.SflKxwRJSMeKKF2QT4abc'}"
    )
    redacted = redact_secrets(text)
    # JWT body は redact されるか Authorization 全体が redact される
    assert "SflKxwRJSMeKKF2QT4abc" not in redacted
    assert "***" in redacted


def test_redacts_authorization_basic() -> None:
    text = "Authorization: Basic dXNlcjpwYXNzd29yZHRoYXRpc2xvbmc="
    redacted = redact_secrets(text)
    assert "dXNlcjpwYXNzd29y" not in redacted
    assert "Authorization" in redacted  # label は保持
    assert "***" in redacted


def test_redacts_x_api_key_header() -> None:
    text = "headers={'x-api-key': 'super-secret-value-here-1234'}"
    redacted = redact_secrets(text)
    assert "super-secret-value" not in redacted


def test_redacts_x_goog_api_key_header() -> None:
    text = "x-goog-api-key: AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q was rejected"
    redacted = redact_secrets(text)
    assert "AIzaSyA1B2C3" not in redacted


def test_redacts_postgres_dsn_credential() -> None:
    text = "DSN postgres://vector:s3cr3tp4ss@db:5432/vector failed"
    redacted = redact_secrets(text)
    assert "s3cr3tp4ss" not in redacted
    assert "postgres://***@db:5432/vector" in redacted


def test_redacts_postgresql_dsn_credential() -> None:
    text = "postgresql://user:pass@host/db"
    redacted = redact_secrets(text)
    assert "user:pass" not in redacted
    assert "postgresql://***@" in redacted


def test_redacts_redis_dsn_credential() -> None:
    text = "redis://default:hunter2redacted@redis:6379"
    redacted = redact_secrets(text)
    assert "hunter2redacted" not in redacted
    assert "redis://***@" in redacted


def test_redacts_naked_jwt() -> None:
    text = (
        "got JWT eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c from upstream"
    )
    redacted = redact_secrets(text)
    assert "eyJhbGciOiJIUzI1NiJ9" not in redacted
    assert "eyJ***" in redacted


# ---------------------------------------------------------------------------
# A-2: 位置・文脈による redact 確認
# ---------------------------------------------------------------------------


def test_redacts_at_string_start() -> None:
    text = "AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q is the leaked key"
    redacted = redact_secrets(text)
    assert redacted.startswith("AIza***")


def test_redacts_at_string_end() -> None:
    text = "key=AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"
    redacted = redact_secrets(text)
    assert redacted.endswith("AIza***")


def test_redacts_inside_multiline_stack_trace() -> None:
    text = (
        "Traceback (most recent call last):\n"
        "  File 'foo.py', line 42, in <module>\n"
        "    client.embed(api_key='AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q')\n"
        "  ValueError: invalid request"
    )
    redacted = redact_secrets(text)
    assert "AIzaSyA1B2C3" not in redacted
    # 改行 + stack 構造が壊れていない
    assert redacted.count("\n") == text.count("\n")
    assert "Traceback" in redacted
    assert "ValueError" in redacted


def test_redacts_inside_json_value() -> None:
    text = (
        '{"api_key": "AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q", "model": "gemini-2.0"}'
    )
    redacted = redact_secrets(text)
    assert "AIzaSyA1B2C3" not in redacted
    # JSON 構造の他要素は保持
    assert '"model": "gemini-2.0"' in redacted


# ---------------------------------------------------------------------------
# A-3: 複数 secret 同居
# ---------------------------------------------------------------------------


def test_redacts_multiple_secret_kinds_in_one_string() -> None:
    text = (
        "creds: AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q "
        "and sk-ant-api03-abcdef0123456789ABCDEFxyz_- "
        "and Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.aaa"
    )
    redacted = redact_secrets(text)
    assert "AIzaSyA1B2C3" not in redacted
    assert "abcdef0123456789" not in redacted
    assert "eyJhbGciOiJIUzI1NiJ9" not in redacted


def test_redacts_same_secret_multiple_times() -> None:
    text = (
        "AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q first occurrence; "
        "AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q second occurrence"
    )
    redacted = redact_secrets(text)
    assert "AIzaSyA1B2C3" not in redacted
    assert redacted.count("AIza***") == 2


# ---------------------------------------------------------------------------
# A-4: anti-test (別 detector で literal 残存を negate)
# ---------------------------------------------------------------------------


_SECRET_DETECTORS = [
    re.compile(r"AIza[A-Za-z0-9_\-]{35}"),
    re.compile(r"sk-(?:ant-|proj-|svcacct-)?[A-Za-z0-9]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    re.compile(r"://[^/@\s]+:[^/@\s]+@"),  # DSN credential
]


@pytest.mark.parametrize(
    "sample",
    [
        "Error: AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q not authorized",
        (
            "Authorization: Bearer "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij"
        ),
        "DSN postgres://user:hunter2redacted@db:5432/x failed",
        ("headers={'x-goog-api-key': 'AIzaSyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX'}"),
        "PAT ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890 invalid",
        "key sk-proj-abcdef0123456789ABCDEFxyz invalid",
    ],
)
def test_no_secret_literal_remains_after_redact(sample: str) -> None:
    """別 detector で走査して既知 secret literal が一切残らないこと。"""
    redacted = redact_secrets(sample)
    for detector in _SECRET_DETECTORS:
        assert not detector.search(redacted), (
            f"secret literal remained: input={sample!r} output={redacted!r} "
            f"detector={detector.pattern!r}"
        )


# ---------------------------------------------------------------------------
# B. 可読性テスト (must-preserve)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sample",
    [
        # 平文 error / stack trace
        "Connection refused on host db.internal port 5432",
        "File '/app/src/observability/recording.py', line 83",
        "Traceback (most recent call last):\n  ValueError: bad input",
        # public URL
        "https://www.anthropic.com/news/claude-4-release",
        "https://github.com/user/repo/issues/123",
        # 32+ char 記事タイトル
        "Anthropic releases Claude 4 with improved coding capability",
        "記事の見出しが長すぎて分析対象外となりました",
        # 数字 / status code
        "HTTP 502 Bad Gateway from upstream",
        "article_id=12345 status=404 retry=3",
        # UUID
        "correlation_id=550e8400-e29b-41d4-a716-446655440000",
        # 日本語混入 error
        "取得に失敗しました: timeout after 30s",
        # JSON without secret
        '{"article_id": 123, "status": "failed", "model": "gemini-2.0"}',
        # DSN without credential (`@` 不在)
        "postgres://db:5432/vector",
        "redis://redis:6379/0",
        # public URL with auth-like path (false positive 候補)
        "GET /repos/owner/Authorization-utils/contents/README returned 200",
        # DeepSeek error 模擬 (sk- prefix が文字列として現れない)
        "DeepSeek API returned: insufficient balance",
    ],
)
def test_normal_text_is_preserved_unchanged(sample: str) -> None:
    """通常テキストは strict equal で無変化。過剰 redact bug の検知。"""
    assert redact_secrets(sample) == sample


# ---------------------------------------------------------------------------
# C. 境界条件
# ---------------------------------------------------------------------------


def test_empty_string_returns_empty() -> None:
    assert redact_secrets("") == ""


def test_long_string_does_not_explode() -> None:
    """1MB 級の入力で catastrophic backtracking しないこと。"""
    payload = "Connection error " * 50_000  # ~900KB
    redacted = redact_secrets(payload)
    assert redacted == payload  # 過剰 redact なし


def test_non_ascii_preserved() -> None:
    """emoji / 日本語 / Cyrillic が壊れない。"""
    sample = "fetch failed 🚫 для пользователя 取得失敗"
    assert redact_secrets(sample) == sample
