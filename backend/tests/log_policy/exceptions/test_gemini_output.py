"""実Gemini SDKの例外をログに渡し、診断を残して応答本文を出さないことを検証する。"""

import json
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from google.genai import errors

from app.ai_providers.errors import AIProviderResponseError, AIProviderResponseReason
from app.analysis.logging import create_article_analysis_logger
from app.http.error_mapping import http_response_error_from_status
from tests.ai_providers.gemini._sdk_exchange import (
    install_sdk_exchange,
    open_gemini_test_client,
)

pytestmark = pytest.mark.unit

_MODEL = "gemini-test-model"
_TEXT = "synthetic user request"


@pytest.fixture
def exchange(monkeypatch):
    """各ケースで設定する応答を実SDKに返す通信模擬を用意する。"""
    return install_sdk_exchange(monkeypatch)


_QUOTA_FAILURE = "type.googleapis.com/google.rpc.QuotaFailure"
_ERROR_INFO = "type.googleapis.com/google.rpc.ErrorInfo"
_HELP = "type.googleapis.com/google.rpc.Help"
_RETRY_INFO = "type.googleapis.com/google.rpc.RetryInfo"
_RECEIVED_AT = datetime(2026, 10, 8, tzinfo=UTC)

# 公開されている無料枠の429の実例にあった枠。
_TOKENS_PER_DAY = "GenerateContentInputTokensPerModelPerDay-FreeTier"
_REQUESTS_PER_DAY = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
_REQUESTS_PER_MINUTE = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
_TOKENS_PER_MINUTE = "GenerateContentInputTokensPerModelPerMinute-FreeTier"


@pytest.fixture
def log_failure(capsys) -> Callable[[BaseException], dict[str, object]]:
    """記事分析のロガーに失敗を渡し、出力された1件を、実行環境で変わる時刻と発生位置を除いて返す。"""

    def _log(error: BaseException) -> dict[str, object]:
        create_article_analysis_logger().error("failed", exc_info=error)
        log = json.loads(capsys.readouterr().out)
        del log["timestamp"], log["frames"]
        for related in log.get("related_exceptions", []):
            del related["exception"]["frames"]
        return log

    return _log


def _caused_by(
    sdk_error: errors.APIError, reason: AIProviderResponseReason
) -> AIProviderResponseError:
    """変換器と同じく、SDK例外を原因に持つAIの例外を作る。"""
    error = AIProviderResponseError(
        reason=reason,
        http_error=http_response_error_from_status(
            sdk_error.code, response=sdk_error.response, received_at=_RECEIVED_AT
        ),
    )
    error.__cause__ = sdk_error
    return error


@pytest.mark.asyncio
async def test_sdk_rate_limit_error_is_logged_with_violations_and_retry_delay(
    exchange, log_failure
):
    """SDKが429応答から起こした例外を原因に持つ失敗をログに渡すと、違反4件と待ち時間が残り、応答本文は出ない。"""
    exchange.status = 429
    exchange.respond_json(
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": (
                    "You exceeded your current quota, please check your plan"
                    " and billing details."
                ),
                "details": [
                    {
                        "@type": _QUOTA_FAILURE,
                        "violations": [
                            {"quotaId": _TOKENS_PER_DAY, "quotaValue": "1000000"},
                            {"quotaId": _REQUESTS_PER_DAY, "quotaValue": "250"},
                            {"quotaId": _REQUESTS_PER_MINUTE, "quotaValue": "10"},
                            {"quotaId": _TOKENS_PER_MINUTE, "quotaValue": "250000"},
                        ],
                    },
                    {
                        "@type": _HELP,
                        "links": [
                            {
                                "description": "Learn more about Gemini API quotas",
                                "url": "https://ai.google.dev/gemini-api/docs/rate-limits",
                            }
                        ],
                    },
                    {"@type": _RETRY_INFO, "retryDelay": "53s"},
                ],
            }
        }
    )
    async with open_gemini_test_client() as client:
        with pytest.raises(errors.ClientError) as caught:
            await client.models.generate_content(model=_MODEL, contents=_TEXT)

    actual_log = log_failure(
        _caused_by(caught.value, AIProviderResponseReason.RATE_LIMITED)
    )

    expected_log = {
        "service": "article_analysis",
        "event": "failed",
        "level": "error",
        "log_policy": "ai_inference",
        "error_class": "app.ai_providers.errors.AIProviderResponseError",
        "error_message": "AIプロバイダーが失敗の応答を返しました",
        "error_details": {
            "code": "ai_provider_response_error",
            "reason": "rate_limited",
        },
        "related_exceptions": [
            {
                "parent": None,
                "relation": "cause",
                "exception": {
                    "error_class": "google.genai.errors.ClientError",
                    "error_message": "Gemini API error: RESOURCE_EXHAUSTED",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 429,
                        "provider_code": "RESOURCE_EXHAUSTED",
                        "quota_violations": [
                            {"quota_id": _TOKENS_PER_DAY, "quota_value": 1000000},
                            {"quota_id": _REQUESTS_PER_DAY, "quota_value": 250},
                            {"quota_id": _REQUESTS_PER_MINUTE, "quota_value": 10},
                            {"quota_id": _TOKENS_PER_MINUTE, "quota_value": 250000},
                        ],
                        "retry_delay_seconds": 53,
                    },
                },
            }
        ],
    }
    assert actual_log == expected_log


@pytest.mark.asyncio
async def test_sdk_invalid_api_key_error_is_logged_with_error_info(
    exchange, log_failure
):
    """SDKがAPIキー不正の応答から起こした例外を原因に持つ失敗をログに渡すと、ErrorInfoが残り、metadataは出ない。"""
    exchange.status = 400
    exchange.respond_json(
        {
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": "API key not valid. Please pass a valid API key.",
                "details": [
                    {
                        "@type": _ERROR_INFO,
                        "reason": "API_KEY_INVALID",
                        "domain": "googleapis.com",
                        "metadata": {"service": "generativelanguage.googleapis.com"},
                    }
                ],
            }
        }
    )
    async with open_gemini_test_client() as client:
        with pytest.raises(errors.ClientError) as caught:
            await client.models.generate_content(model=_MODEL, contents=_TEXT)

    actual_log = log_failure(_caused_by(caught.value, AIProviderResponseReason.AUTH))

    expected_log = {
        "service": "article_analysis",
        "event": "failed",
        "level": "error",
        "log_policy": "ai_inference",
        "error_class": "app.ai_providers.errors.AIProviderResponseError",
        "error_message": "AIプロバイダーが失敗の応答を返しました",
        "error_details": {
            "code": "ai_provider_response_error",
            "reason": "auth",
        },
        "related_exceptions": [
            {
                "parent": None,
                "relation": "cause",
                "exception": {
                    "error_class": "google.genai.errors.ClientError",
                    "error_message": (
                        "Gemini API error: INVALID_ARGUMENT / API_KEY_INVALID"
                    ),
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 400,
                        "provider_code": "INVALID_ARGUMENT",
                        "error_info": {
                            "reason": "API_KEY_INVALID",
                            "domain": "googleapis.com",
                        },
                    },
                },
            }
        ],
    }
    assert actual_log == expected_log


@pytest.mark.asyncio
async def test_sdk_non_json_server_error_is_logged_without_body(exchange, log_failure):
    """SDKがJSONでない503応答から起こした例外を原因に持つ失敗をログに渡すと、HTTPの数字だけが残り、本文は出ない。"""
    exchange.status = 503
    exchange.headers = [(b"content-type", b"text/html")]
    exchange.body.chunks = (b"<html>Service Unavailable</html>",)
    async with open_gemini_test_client() as client:
        with pytest.raises(errors.ServerError) as caught:
            await client.models.generate_content(model=_MODEL, contents=_TEXT)

    actual_log = log_failure(
        _caused_by(caught.value, AIProviderResponseReason.SERVER_ERROR)
    )

    expected_log = {
        "service": "article_analysis",
        "event": "failed",
        "level": "error",
        "log_policy": "ai_inference",
        "error_class": "app.ai_providers.errors.AIProviderResponseError",
        "error_message": "AIプロバイダーが失敗の応答を返しました",
        "error_details": {
            "code": "ai_provider_response_error",
            "reason": "server_error",
        },
        "related_exceptions": [
            {
                "parent": None,
                "relation": "cause",
                "exception": {
                    "error_class": "google.genai.errors.ServerError",
                    "error_message": "Gemini API error",
                    "error_details": {"kind": "gemini", "http_status": 503},
                },
            }
        ],
    }
    assert actual_log == expected_log


@pytest.mark.asyncio
async def test_sdk_error_inside_stream_is_logged_with_http_200_and_status(
    exchange, log_failure
):
    """SDKがストリームの中の429から起こした例外をログに渡すと、HTTPの200とGeminiのstatusが残る。"""
    exchange.respond_sse(
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "Resource has been exhausted (e.g. check quota).",
            }
        }
    )
    async with open_gemini_test_client() as client:
        stream = await client.models.generate_content_stream(
            model=_MODEL, contents=_TEXT
        )
        with pytest.raises(errors.ClientError) as caught:
            await anext(stream)

    actual_log = log_failure(caught.value)

    expected_log = {
        "service": "article_analysis",
        "event": "failed",
        "level": "error",
        "log_policy": "ai_inference",
        "error_class": "google.genai.errors.ClientError",
        "error_message": "Gemini API error: RESOURCE_EXHAUSTED",
        "error_details": {
            "kind": "gemini",
            "http_status": 200,
            "provider_code": "RESOURCE_EXHAUSTED",
        },
    }
    assert actual_log == expected_log


@pytest.mark.asyncio
async def test_sdk_unparseable_stream_is_logged_with_decode_cause(
    exchange, log_failure
):
    """SDKがストリームの不正JSONから起こした例外をログに渡すと、固定文と解析失敗の原因が残り、生の応答は出ない。"""
    exchange.headers = [(b"content-type", b"text/event-stream")]
    exchange.body.chunks = (b"data: <html>Service Unavailable</html>\n\n",)
    async with open_gemini_test_client() as client:
        stream = await client.models.generate_content_stream(
            model=_MODEL, contents=_TEXT
        )
        with pytest.raises(errors.UnknownApiResponseError) as caught:
            await anext(stream)

    actual_log = log_failure(caught.value)

    expected_log = {
        "service": "article_analysis",
        "event": "failed",
        "level": "error",
        "log_policy": "ai_inference",
        "error_class": "google.genai.errors.UnknownApiResponseError",
        "error_message": "Gemini API response could not be parsed as JSON",
        "related_exceptions": [
            {
                "parent": None,
                "relation": "cause",
                "exception": {
                    "error_class": "json.decoder.JSONDecodeError",
                    "error_message": "Expecting value: line 1 column 1 (char 0)",
                },
            }
        ],
    }
    assert actual_log == expected_log
