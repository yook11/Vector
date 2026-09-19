"""内容に基づく秘密情報の検出。"""

from __future__ import annotations

import pytest

from app.log_policy import sanitize
from app.log_policy.budget import TEXT_LIMIT
from app.log_policy.sanitize import sanitize_text

pytestmark = pytest.mark.unit


def test_private_key_block_is_removed_as_a_whole() -> None:
    """PEM 秘密鍵は BEGIN から END までを本文ごと伏せ、前後の文は残す。"""
    secret = "synthetic private value"
    text = f"failed -----BEGIN PRIVATE KEY-----\n{secret}\n-----END PRIVATE KEY-----"
    assert sanitize_text(text) == "failed ***"


def test_gemini_key_is_replaced_with_context_kept() -> None:
    """Geminiキーの既知形式を置換し、周囲の原因文を残す。"""
    # 合成値を分割し、秘密検出ツールの規則に一致させない。
    text = "Error from AIza" + "SyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"
    expected = "Error from AIza***"
    assert sanitize_text(text) == expected


def test_anthropic_key_is_replaced_with_context_kept() -> None:
    """Anthropicキーの既知形式を置換し、周囲の原因文を残す。"""
    text = "key=sk-ant-api03-abcdef0123456789ABCDEFxyz_-X failed"
    expected = "key=sk-ant-*** failed"
    assert sanitize_text(text) == expected


def test_openai_key_is_replaced_with_context_kept() -> None:
    """OpenAIキーの既知形式を置換し、周囲の原因文を残す。"""
    text = "sk-proj-abcdef0123456789ABCDEFxyz used"
    expected = "sk-*** used"
    assert sanitize_text(text) == expected


def test_github_token_is_replaced_with_context_kept() -> None:
    """GitHubトークンの既知形式を置換し、周囲の原因文を残す。"""
    text = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890 on /repos"
    expected = "gh*_*** on /repos"
    assert sanitize_text(text) == expected


def test_tavily_key_is_replaced_with_context_kept() -> None:
    """Tavilyキーの既知形式を置換し、周囲の原因文を残す。"""
    text = "tvly-abcdefghijklmnopqrstuvwxyz0123 with 401"
    expected = "tvly-*** with 401"
    assert sanitize_text(text) == expected


def test_gemini_key_shorter_than_detection_length_is_preserved() -> None:
    """Geminiキー形式の長さに届かない文字列を部分置換しない。"""
    text = "Error from AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
    assert sanitize.sanitize_provider_keys(text) == text


def test_github_token_shorter_than_detection_length_is_preserved() -> None:
    """GitHubトークン形式の長さに届かない文字列を部分置換しない。"""
    text = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ123456789 on /repos"
    assert sanitize.sanitize_provider_keys(text) == text


def test_openai_key_shorter_than_detection_length_is_preserved() -> None:
    """OpenAIキー形式の長さに届かない文字列を部分置換しない。"""
    text = "sk-abcdefghijklmnopqrs used"
    assert sanitize.sanitize_provider_keys(text) == text


def test_postgres_dsn_userinfo_is_hidden_with_endpoint_kept() -> None:
    """driver付きPostgreSQL接続文字列の認証部分を伏せ、接続先を残す。"""
    text = "postgresql+asyncpg://vector:s3cr3tp4ss@db:5432/vector"
    expected = "postgresql+asyncpg://***@db:5432/vector"
    assert sanitize_text(text) == expected


def test_redis_url_userinfo_is_hidden_with_endpoint_kept() -> None:
    """Redis接続URLの認証部分を伏せ、接続先を残す。"""
    text = "rediss://default:hunter2redacted@cache:6380/0"
    expected = "rediss://***@cache:6380/0"
    assert sanitize_text(text) == expected


def test_connection_error_without_credentials_is_preserved() -> None:
    """認証値を含まない接続エラーの原因文を残す。"""
    sample = "Connection refused on host db.internal port 5432"
    assert sanitize_text(sample) == sample


def test_public_article_url_is_preserved() -> None:
    """認証値を含まない公開記事URLを残す。"""
    sample = "https://www.anthropic.com/news/claude-4-release"
    assert sanitize_text(sample) == sample


def test_postgres_url_without_userinfo_is_preserved() -> None:
    """認証部分のないPostgreSQL接続URLを残す。"""
    sample = "postgres://db:5432/vector"
    assert sanitize_text(sample) == sample


def test_aws_arn_is_preserved() -> None:
    """AWSのARNは認証値ではなく識別情報として残す。"""
    sample = "arn:aws:ssm:ap-northeast-1:123456789012:parameter/vector/db"
    assert sanitize_text(sample) == sample


def test_rds_endpoint_is_preserved() -> None:
    """RDSの接続先とポートを識別情報として残す。"""
    sample = "vector-db.cluster-abc.ap-northeast-1.rds.amazonaws.com:5432"
    assert sanitize_text(sample) == sample


def test_japanese_error_message_is_preserved() -> None:
    """認証値を含まない日本語の原因文を残す。"""
    sample = "取得に失敗しました: timeout after 30s"
    assert sanitize_text(sample) == sample


def test_sanitizer_preserves_length_until_output_protection() -> None:
    """サニタイズは置換のみを担い、文字数制限を暗黙適用しない。"""
    text = "y" * (TEXT_LIMIT * 3)
    assert sanitize_text(text) == text


def test_sanitization_does_not_mask_by_assignment_key() -> None:
    """内容から識別できない値は認証キー付きでもsanitize単体では置換しない。"""
    text = "password='synthetic private value'"
    assert sanitize_text(text) == text
