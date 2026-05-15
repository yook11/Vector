"""``http_error_translation`` の写像 SSoT を表駆動で固定するテスト。

- ``classify_fetch_status``: status → origin error の全 row を固定する。特に
  401 / 403 → ``FetchAccessDeniedError`` を明示的に lock し、現 html_extractor の
  401 分類ズレを Stage 1 経路で正規化する意図をテスト化する。
- ``translate_fetch_exception``: httpx / SSRF guard 例外の振り分けを固定する
  (``TimeoutException`` を ``RequestError`` より先に判定することを含む)。
- ``RECOVERABLE_FETCH_ERRORS``: 旧 ``TemporaryFetchError`` 発生条件の忠実訳で
  あることを membership で固定する。
"""

from __future__ import annotations

import httpx
import pytest

from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchGatewayError,
    FetchLegalBlockError,
    FetchNetworkError,
    FetchOriginServerError,
    FetchRateLimitedError,
    FetchRequestTimeoutError,
    FetchResourceNotFoundError,
    FetchRetryableStatusError,
    FetchSsrfBlockedError,
    FetchTimeoutError,
    FetchUnexpectedStatusError,
)
from app.collection.fetchers.tools.http_error_translation import (
    RECOVERABLE_FETCH_ERRORS,
    classify_fetch_status,
    translate_fetch_exception,
)
from app.shared.security.ssrf_guard import HostBlockedError, HostResolutionError


@pytest.mark.parametrize(
    "status,exp_cls,exp_attrs",
    [
        (401, FetchAccessDeniedError, {"status_code": 401, "reason": "unauthorized"}),
        (403, FetchAccessDeniedError, {"status_code": 403, "reason": "forbidden"}),
        (451, FetchLegalBlockError, {"status_code": 451}),
        (404, FetchResourceNotFoundError, {"status_code": 404, "reason": "not_found"}),
        (410, FetchResourceNotFoundError, {"status_code": 410, "reason": "gone"}),
        (429, FetchRateLimitedError, {"status_code": 429}),
        (
            500,
            FetchOriginServerError,
            {"status_code": 500, "reason": "internal_error"},
        ),
        (
            503,
            FetchOriginServerError,
            {"status_code": 503, "reason": "service_unavailable"},
        ),
        (502, FetchGatewayError, {"status_code": 502}),
        (504, FetchGatewayError, {"status_code": 504}),
        (408, FetchRequestTimeoutError, {"status_code": 408}),
        (425, FetchRetryableStatusError, {"status_code": 425, "reason": "too_early"}),
        (418, FetchUnexpectedStatusError, {"status_code": 418}),
        (400, FetchUnexpectedStatusError, {"status_code": 400}),
        (422, FetchUnexpectedStatusError, {"status_code": 422}),
    ],
)
def test_classify_fetch_status_table(
    status: int,
    exp_cls: type,
    exp_attrs: dict[str, object],
) -> None:
    """status → origin error の対応表を全 row 固定する。"""
    err = classify_fetch_status(status, {})
    assert type(err) is exp_cls
    for attr, value in exp_attrs.items():
        assert getattr(err, attr) == value


def test_classify_429_extracts_retry_after() -> None:
    err = classify_fetch_status(429, {"Retry-After": "90"})
    assert isinstance(err, FetchRateLimitedError)
    assert err.retry_after_seconds == 90.0


def test_classify_503_extracts_retry_after_but_500_does_not() -> None:
    err503 = classify_fetch_status(503, {"retry-after": "30"})
    err500 = classify_fetch_status(500, {"Retry-After": "30"})
    assert isinstance(err503, FetchOriginServerError)
    assert err503.retry_after_seconds == 30.0
    assert isinstance(err500, FetchOriginServerError)
    assert err500.retry_after_seconds is None


def test_classify_retry_after_http_date_is_ignored() -> None:
    """HTTP-date 形式の Retry-After は本 stage では parse せず None。"""
    err = classify_fetch_status(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert isinstance(err, FetchRateLimitedError)
    assert err.retry_after_seconds is None


def _http_status_error(status: int, headers: dict[str, str]) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://example.com/feed")
    resp = httpx.Response(status, headers=headers, request=req)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


def test_translate_http_status_error_delegates_to_classify() -> None:
    err = translate_fetch_exception(
        _http_status_error(503, {"Retry-After": "120"}),
        source_name="VentureBeat",
    )
    assert isinstance(err, FetchOriginServerError)
    assert err.status_code == 503
    assert err.retry_after_seconds == 120.0


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("slow connect"),
        httpx.ReadTimeout("slow read"),
        httpx.PoolTimeout("pool exhausted"),
    ],
    ids=["connect_timeout", "read_timeout", "pool_timeout"],
)
def test_translate_timeout_before_request_error(exc: httpx.TimeoutException) -> None:
    """``TimeoutException`` は ``RequestError`` の subclass。先に判定される。"""
    err = translate_fetch_exception(exc, source_name="S")
    assert isinstance(err, FetchTimeoutError)


def test_translate_non_timeout_request_error_is_network() -> None:
    err = translate_fetch_exception(
        httpx.ConnectError("connection refused"), source_name="S"
    )
    assert isinstance(err, FetchNetworkError)


def test_translate_host_blocked_is_ssrf() -> None:
    err = translate_fetch_exception(
        HostBlockedError("blocked private ip 10.0.0.1"), source_name="S"
    )
    assert isinstance(err, FetchSsrfBlockedError)


def test_translate_host_resolution_is_network() -> None:
    err = translate_fetch_exception(
        HostResolutionError("dns NXDOMAIN"), source_name="S"
    )
    assert isinstance(err, FetchNetworkError)


def test_translate_unknown_exception_falls_back_to_network() -> None:
    """呼出側 except が広すぎて未知例外が来ても翻訳自体は落ちない。"""
    err = translate_fetch_exception(ValueError("surprise"), source_name="S")
    assert isinstance(err, FetchNetworkError)


def test_recoverable_fetch_errors_composition() -> None:
    """旧 ``TemporaryFetchError`` 発生条件の忠実訳であることを固定する。"""
    assert set(RECOVERABLE_FETCH_ERRORS) == {
        FetchTimeoutError,
        FetchNetworkError,
        FetchOriginServerError,
        FetchGatewayError,
        FetchRequestTimeoutError,
        FetchRateLimitedError,
        FetchRetryableStatusError,
        FetchUnexpectedStatusError,
    }
    # bubble 側 (source 全体失敗) は recoverable に含めない。
    assert FetchAccessDeniedError not in RECOVERABLE_FETCH_ERRORS
    assert FetchResourceNotFoundError not in RECOVERABLE_FETCH_ERRORS
    assert FetchLegalBlockError not in RECOVERABLE_FETCH_ERRORS
    assert FetchSsrfBlockedError not in RECOVERABLE_FETCH_ERRORS
