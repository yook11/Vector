"""Gemini SDK例外1件を、応答の自由文を含まない原因情報へ変換することを検証する。"""

import json
from dataclasses import asdict

import httpx2
import pytest
from google.genai import errors as genai_errors

from app.log_policy.exceptions.conversion import convert_exception

pytestmark = pytest.mark.unit

# 識別子の形に合う値にし、形の検査だけでは除けないことを確かめる。
_SECRET = "synthetic-private"

# google.rpc のエラー詳細の型（error_details.proto）。
_QUOTA_FAILURE = "type.googleapis.com/google.rpc.QuotaFailure"
_ERROR_INFO = "type.googleapis.com/google.rpc.ErrorInfo"
_HELP = "type.googleapis.com/google.rpc.Help"
_LOCALIZED_MESSAGE = "type.googleapis.com/google.rpc.LocalizedMessage"
_BAD_REQUEST = "type.googleapis.com/google.rpc.BadRequest"
_RETRY_INFO = "type.googleapis.com/google.rpc.RetryInfo"
_QUOTA_ID = "GenerateRequestsPerMinutePerProjectPerModel"
_DAILY_QUOTA_ID = "GenerateRequestsPerDayPerProjectPerModel"


class TestKnownResponseTextIsNotOutput:
    """Geminiが返すと分かっている項目のうち、出さないと決めたものは変換結果に出ない。"""

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            pytest.param(
                genai_errors.ClientError(
                    400,
                    {
                        "error": {
                            "code": 400,
                            "status": "INVALID_ARGUMENT",
                            "message": _SECRET,
                        }
                    },
                    response=httpx2.Response(400),
                ),
                {
                    "message": "Gemini API error: INVALID_ARGUMENT",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 400,
                        "provider_code": "INVALID_ARGUMENT",
                    },
                    "cause_is_aggregated": False,
                },
                id="message",
            ),
            pytest.param(
                genai_errors.ClientError(
                    429,
                    {
                        "error": {
                            "code": 429,
                            "status": "RESOURCE_EXHAUSTED",
                            "details": [
                                {
                                    "@type": _QUOTA_FAILURE,
                                    "violations": [
                                        {"quotaId": _QUOTA_ID, "description": _SECRET}
                                    ],
                                }
                            ],
                        }
                    },
                    response=httpx2.Response(429),
                ),
                {
                    "message": "Gemini API error: RESOURCE_EXHAUSTED",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 429,
                        "provider_code": "RESOURCE_EXHAUSTED",
                        "quota_violations": [{"quota_id": _QUOTA_ID}],
                    },
                    "cause_is_aggregated": False,
                },
                id="quota_violation_description",
            ),
            pytest.param(
                genai_errors.ClientError(
                    429,
                    {
                        "error": {
                            "code": 429,
                            "status": "RESOURCE_EXHAUSTED",
                            "details": [
                                {
                                    "@type": _QUOTA_FAILURE,
                                    "violations": [
                                        {"quotaId": _QUOTA_ID, "quotaMetric": _SECRET}
                                    ],
                                }
                            ],
                        }
                    },
                    response=httpx2.Response(429),
                ),
                {
                    "message": "Gemini API error: RESOURCE_EXHAUSTED",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 429,
                        "provider_code": "RESOURCE_EXHAUSTED",
                        "quota_violations": [{"quota_id": _QUOTA_ID}],
                    },
                    "cause_is_aggregated": False,
                },
                id="quota_metric",
            ),
            pytest.param(
                genai_errors.ClientError(
                    429,
                    {
                        "error": {
                            "code": 429,
                            "status": "RESOURCE_EXHAUSTED",
                            "details": [
                                {
                                    "@type": _QUOTA_FAILURE,
                                    "violations": [
                                        {
                                            "quotaId": _QUOTA_ID,
                                            "quotaDimensions": {_SECRET: _SECRET},
                                        }
                                    ],
                                }
                            ],
                        }
                    },
                    response=httpx2.Response(429),
                ),
                {
                    "message": "Gemini API error: RESOURCE_EXHAUSTED",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 429,
                        "provider_code": "RESOURCE_EXHAUSTED",
                        "quota_violations": [{"quota_id": _QUOTA_ID}],
                    },
                    "cause_is_aggregated": False,
                },
                id="quota_dimensions",
            ),
            pytest.param(
                genai_errors.ClientError(
                    400,
                    {
                        "error": {
                            "code": 400,
                            "status": "INVALID_ARGUMENT",
                            "details": [
                                {
                                    "@type": _ERROR_INFO,
                                    "reason": "API_KEY_INVALID",
                                    "domain": "googleapis.com",
                                    "metadata": {_SECRET: _SECRET},
                                }
                            ],
                        }
                    },
                    response=httpx2.Response(400),
                ),
                {
                    "message": "Gemini API error: INVALID_ARGUMENT / API_KEY_INVALID",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 400,
                        "provider_code": "INVALID_ARGUMENT",
                        "error_info": {
                            "reason": "API_KEY_INVALID",
                            "domain": "googleapis.com",
                        },
                    },
                    "cause_is_aggregated": False,
                },
                id="error_info_metadata",
            ),
            pytest.param(
                genai_errors.ClientError(
                    400,
                    {
                        "error": {
                            "code": 400,
                            "status": "INVALID_ARGUMENT",
                            "details": [
                                {
                                    "@type": _HELP,
                                    "links": [
                                        {
                                            "description": _SECRET,
                                            "url": f"https://example.com/{_SECRET}",
                                        }
                                    ],
                                }
                            ],
                        }
                    },
                    response=httpx2.Response(400),
                ),
                {
                    "message": "Gemini API error: INVALID_ARGUMENT",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 400,
                        "provider_code": "INVALID_ARGUMENT",
                    },
                    "cause_is_aggregated": False,
                },
                id="help_links",
            ),
            pytest.param(
                genai_errors.ClientError(
                    400,
                    {
                        "error": {
                            "code": 400,
                            "status": "INVALID_ARGUMENT",
                            "details": [
                                {
                                    "@type": _LOCALIZED_MESSAGE,
                                    "locale": "en-US",
                                    "message": _SECRET,
                                }
                            ],
                        }
                    },
                    response=httpx2.Response(400),
                ),
                {
                    "message": "Gemini API error: INVALID_ARGUMENT",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 400,
                        "provider_code": "INVALID_ARGUMENT",
                    },
                    "cause_is_aggregated": False,
                },
                id="localized_message",
            ),
            pytest.param(
                genai_errors.ClientError(
                    400,
                    {
                        "error": {
                            "code": 400,
                            "status": "INVALID_ARGUMENT",
                            "details": [
                                {
                                    "@type": _BAD_REQUEST,
                                    "fieldViolations": [
                                        {"field": _SECRET, "description": _SECRET}
                                    ],
                                }
                            ],
                        }
                    },
                    response=httpx2.Response(400),
                ),
                {
                    "message": "Gemini API error: INVALID_ARGUMENT",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 400,
                        "provider_code": "INVALID_ARGUMENT",
                    },
                    "cause_is_aggregated": False,
                },
                id="bad_request_field",
            ),
            pytest.param(
                # JSONでない応答では、SDKが本文をmessageに、理由句をstatusに入れる。
                genai_errors.ServerError(
                    503,
                    {"message": _SECRET, "status": "Service Unavailable"},
                    response=httpx2.Response(503),
                ),
                {
                    "message": "Gemini API error",
                    "error_details": {"kind": "gemini", "http_status": 503},
                    "cause_is_aggregated": False,
                },
                id="non_json_body",
            ),
            pytest.param(
                genai_errors.UnknownApiResponseError(_SECRET),
                {
                    "message": "Gemini API response could not be parsed as JSON",
                    "error_details": None,
                    "cause_is_aggregated": False,
                },
                id="unparseable_response",
            ),
        ],
    )
    def test_omitted_text_is_not_output(
        self, exc: Exception, expected: dict[str, object]
    ) -> None:
        """出さないと決めた項目に値があっても、変換結果は使うと決めた項目だけになる。"""
        result = convert_exception(exc)

        assert asdict(result) == expected


class TestWantedDiagnosticsAreKept:
    """Geminiが返しうる形の応答では、残すと決めた診断が変換結果に残る。"""

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            pytest.param(
                genai_errors.ClientError(
                    403,
                    {"error": {"code": 403, "status": "PERMISSION_DENIED"}},
                    response=httpx2.Response(403),
                ),
                {
                    "message": "Gemini API error: PERMISSION_DENIED",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 403,
                        "provider_code": "PERMISSION_DENIED",
                    },
                    "cause_is_aggregated": False,
                },
                id="status_and_http_status",
            ),
            pytest.param(
                # ストリームはHTTP 200で始まり、途中の本文で429を返す。
                genai_errors.ClientError(
                    429,
                    {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}},
                    response=httpx2.Response(200),
                ),
                {
                    "message": "Gemini API error: RESOURCE_EXHAUSTED",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 200,
                        "provider_code": "RESOURCE_EXHAUSTED",
                    },
                    "cause_is_aggregated": False,
                },
                id="error_inside_stream",
            ),
            pytest.param(
                genai_errors.ClientError(
                    400,
                    {
                        "error": {
                            "code": 400,
                            "status": "INVALID_ARGUMENT",
                            "details": [
                                {
                                    "@type": _ERROR_INFO,
                                    "reason": "API_KEY_INVALID",
                                    "domain": "googleapis.com",
                                }
                            ],
                        }
                    },
                    response=httpx2.Response(400),
                ),
                {
                    "message": "Gemini API error: INVALID_ARGUMENT / API_KEY_INVALID",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 400,
                        "provider_code": "INVALID_ARGUMENT",
                        "error_info": {
                            "reason": "API_KEY_INVALID",
                            "domain": "googleapis.com",
                        },
                    },
                    "cause_is_aggregated": False,
                },
                id="error_info",
            ),
            pytest.param(
                # quotaValue は int64 で、ProtoJSON では数字の文字列になる。
                genai_errors.ClientError(
                    429,
                    {
                        "error": {
                            "code": 429,
                            "status": "RESOURCE_EXHAUSTED",
                            "details": [
                                {
                                    "@type": _QUOTA_FAILURE,
                                    "violations": [
                                        {"quotaId": _QUOTA_ID, "quotaValue": "1000"},
                                        {
                                            "quotaId": _DAILY_QUOTA_ID,
                                            "quotaValue": "10000",
                                        },
                                    ],
                                }
                            ],
                        }
                    },
                    response=httpx2.Response(429),
                ),
                {
                    "message": "Gemini API error: RESOURCE_EXHAUSTED",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 429,
                        "provider_code": "RESOURCE_EXHAUSTED",
                        "quota_violations": [
                            {"quota_id": _QUOTA_ID, "quota_value": 1000},
                            {"quota_id": _DAILY_QUOTA_ID, "quota_value": 10000},
                        ],
                    },
                    "cause_is_aggregated": False,
                },
                id="quota_violations",
            ),
            pytest.param(
                genai_errors.ClientError(
                    429,
                    {
                        "error": {
                            "code": 429,
                            "status": "RESOURCE_EXHAUSTED",
                            "details": [{"@type": _RETRY_INFO, "retryDelay": "53s"}],
                        }
                    },
                    response=httpx2.Response(429),
                ),
                {
                    "message": "Gemini API error: RESOURCE_EXHAUSTED",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 429,
                        "provider_code": "RESOURCE_EXHAUSTED",
                        "retry_delay_seconds": 53,
                    },
                    "cause_is_aggregated": False,
                },
                id="retry_delay",
            ),
            pytest.param(
                # Duration は ProtoJSON で小数部を0・3・6・9桁で出す。
                genai_errors.ServerError(
                    503,
                    {
                        "error": {
                            "code": 503,
                            "status": "UNAVAILABLE",
                            "details": [{"@type": _RETRY_INFO, "retryDelay": "0.500s"}],
                        }
                    },
                    response=httpx2.Response(503),
                ),
                {
                    "message": "Gemini API error: UNAVAILABLE",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 503,
                        "provider_code": "UNAVAILABLE",
                        "retry_delay_seconds": 0.5,
                    },
                    "cause_is_aggregated": False,
                },
                id="fractional_retry_delay",
            ),
            pytest.param(
                # 詳細は型で区別し、並び順は決まっていない。
                genai_errors.ClientError(
                    429,
                    {
                        "error": {
                            "code": 429,
                            "status": "RESOURCE_EXHAUSTED",
                            "details": [
                                {"@type": _RETRY_INFO, "retryDelay": "53s"},
                                {
                                    "@type": _HELP,
                                    "links": [
                                        {
                                            "description": "Learn more",
                                            "url": "https://ai.google.dev/",
                                        }
                                    ],
                                },
                                {
                                    "@type": _QUOTA_FAILURE,
                                    "violations": [{"quotaId": _QUOTA_ID}],
                                },
                            ],
                        }
                    },
                    response=httpx2.Response(429),
                ),
                {
                    "message": "Gemini API error: RESOURCE_EXHAUSTED",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 429,
                        "provider_code": "RESOURCE_EXHAUSTED",
                        "quota_violations": [{"quota_id": _QUOTA_ID}],
                        "retry_delay_seconds": 53,
                    },
                    "cause_is_aggregated": False,
                },
                id="details_in_any_order",
            ),
            pytest.param(
                genai_errors.ClientError(
                    400,
                    {
                        "error": {
                            "code": 400,
                            "status": "INVALID_ARGUMENT",
                            "message": (
                                "The input token count (1250000) exceeds the maximum"
                                " number of tokens allowed (1048576)."
                            ),
                        }
                    },
                    response=httpx2.Response(400),
                ),
                {
                    "message": "Gemini API error: INVALID_ARGUMENT",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 400,
                        "provider_code": "INVALID_ARGUMENT",
                        "input_tokens": 1250000,
                        "max_input_tokens": 1048576,
                    },
                    "cause_is_aggregated": False,
                },
                id="input_token_counts",
            ),
            pytest.param(
                # 値の一覧では絞らないので、知らない reason や枠も形が合えば残る。
                genai_errors.ClientError(
                    429,
                    {
                        "error": {
                            "code": 429,
                            "status": "RESOURCE_EXHAUSTED",
                            "details": [
                                {
                                    "@type": _ERROR_INFO,
                                    "reason": "NEW_REASON",
                                    "domain": "generativelanguage.googleapis.com",
                                },
                                {
                                    "@type": _QUOTA_FAILURE,
                                    "violations": [
                                        {"quotaId": "NewQuotaPerHourPerProject"}
                                    ],
                                },
                            ],
                        }
                    },
                    response=httpx2.Response(429),
                ),
                {
                    "message": "Gemini API error: RESOURCE_EXHAUSTED / NEW_REASON",
                    "error_details": {
                        "kind": "gemini",
                        "http_status": 429,
                        "provider_code": "RESOURCE_EXHAUSTED",
                        "error_info": {
                            "reason": "NEW_REASON",
                            "domain": "generativelanguage.googleapis.com",
                        },
                        "quota_violations": [{"quota_id": "NewQuotaPerHourPerProject"}],
                    },
                    "cause_is_aggregated": False,
                },
                id="unknown_identifiers",
            ),
        ],
    )
    def test_wanted_diagnostics_are_kept(
        self, exc: Exception, expected: dict[str, object]
    ) -> None:
        """残すと決めた診断が、決めた項目名で残る。"""
        result = convert_exception(exc)

        assert asdict(result) == expected


class TestUnexpectedInputIsHandledSafely:
    """Geminiが返すと考えにくい形の値が混ざっても、その値は出さず、変換も止めない。"""

    def test_free_text_in_identifier_fields_is_not_output(self) -> None:
        """識別子の項目に自由文が入っても、その値は出ない。"""
        free_text = f"contact {_SECRET} for details"
        exc = genai_errors.ClientError(
            429,
            {
                "error": {
                    "code": 429,
                    "status": free_text,
                    "details": [
                        {
                            "@type": _ERROR_INFO,
                            "reason": free_text,
                            "domain": free_text,
                        },
                        {
                            "@type": _QUOTA_FAILURE,
                            "violations": [{"quotaId": free_text}],
                        },
                        {"@type": _RETRY_INFO, "retryDelay": free_text},
                    ],
                }
            },
            response=httpx2.Response(429),
        )

        result = convert_exception(exc)

        assert _SECRET not in json.dumps(asdict(result), ensure_ascii=False)

    @pytest.mark.parametrize(
        ("exc", "expected_message"),
        [
            pytest.param(
                genai_errors.ClientError(
                    400,
                    {
                        "error": {
                            "code": 400,
                            "status": "INVALID_ARGUMENT",
                            "details": [
                                "not a detail",
                                42,
                                {"@type": _ERROR_INFO, "reason": 123, "domain": []},
                                {"@type": _QUOTA_FAILURE, "violations": "none"},
                                {"@type": _RETRY_INFO, "retryDelay": 53},
                            ],
                        }
                    },
                    response=httpx2.Response(400),
                ),
                "Gemini API error: INVALID_ARGUMENT",
                id="details_of_unexpected_type",
            ),
            pytest.param(
                genai_errors.ClientError(
                    400,
                    {"error": {"code": 400, "status": {"unexpected": "shape"}}},
                    response=httpx2.Response(400),
                ),
                "Gemini API error",
                id="status_of_unexpected_type",
            ),
        ],
    )
    def test_values_of_unexpected_type_do_not_break_conversion(
        self, exc: Exception, expected_message: str
    ) -> None:
        """型の違う値が混ざっても、その値を捨てて変換を続け、原因の文を作る。"""
        result = convert_exception(exc)

        assert result.message == expected_message
