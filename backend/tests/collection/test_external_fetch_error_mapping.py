"""``external_fetch_error_mapping`` の写像 SSoT を表駆動で固定するテスト。

- ``external_fetch_error_from_http_status``: status → origin error の全 row を固定
  する。特に 401 / 403 → ``FetchAccessDeniedError`` を明示的に lock し、旧 extractor
  (article_completion) の 401 分類ズレを Stage 1 経路で正規化する意図をテスト化する。
  明示表に無い status は ``status_code // 100`` で割って 3xx=redirect blocked /
  5xx=server / それ以外=client に倒すことを固定する。
- ``external_fetch_error_from_exception``: httpx / SSRF guard 例外の振り分けを固定する
  (``TimeoutException`` を ``RequestError`` より先に判定することを含む)。

retryable / terminal の分類は origin error 自身の ``retryable`` 属性 (SSoT) が
持ち、その CODE 集合の spec-lock は ``test_external_fetch_error_codes.py`` が所有
する。本 module は status / 例外 → origin error の写像のみを扱う。
"""

from __future__ import annotations

import httpx
import pytest

from app.collection.external_fetch_error_mapping import (
    external_fetch_error_from_exception,
    external_fetch_error_from_http_status,
)
from app.collection.external_fetch_errors import (
    FetchAccessDeniedError,
    FetchGatewayError,
    FetchLegalBlockError,
    FetchNetworkError,
    FetchOriginServerError,
    FetchRateLimitedError,
    FetchRedirectBlockedError,
    FetchRequestTimeoutError,
    FetchResourceNotFoundError,
    FetchRetryableStatusError,
    FetchSsrfBlockedError,
    FetchTimeoutError,
    FetchUnexpectedClientStatusError,
    FetchUnexpectedServerStatusError,
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
        # 表外 4xx は terminal な client status に倒す (旧 retryable=True からの是正)。
        (
            400,
            FetchUnexpectedClientStatusError,
            {"status_code": 400, "retryable": False},
        ),
        (
            409,
            FetchUnexpectedClientStatusError,
            {"status_code": 409, "retryable": False},
        ),
        (
            413,
            FetchUnexpectedClientStatusError,
            {"status_code": 413, "retryable": False},
        ),
        (
            418,
            FetchUnexpectedClientStatusError,
            {"status_code": 418, "retryable": False},
        ),
        (
            422,
            FetchUnexpectedClientStatusError,
            {"status_code": 422, "retryable": False},
        ),
        # 3xx は全経路で redirect blocked (terminal)。
        (301, FetchRedirectBlockedError, {"status_code": 301, "retryable": False}),
        (302, FetchRedirectBlockedError, {"status_code": 302, "retryable": False}),
        (307, FetchRedirectBlockedError, {"status_code": 307, "retryable": False}),
        (308, FetchRedirectBlockedError, {"status_code": 308, "retryable": False}),
        # 表外 5xx は retryable な server status。
        (
            501,
            FetchUnexpectedServerStatusError,
            {"status_code": 501, "retryable": True},
        ),
        (
            507,
            FetchUnexpectedServerStatusError,
            {"status_code": 507, "retryable": True},
        ),
        (
            520,
            FetchUnexpectedServerStatusError,
            {"status_code": 520, "retryable": True},
        ),
        # 1xx / 範囲外 は分類不能 status として terminal な client status に倒す。
        (
            100,
            FetchUnexpectedClientStatusError,
            {"status_code": 100, "retryable": False},
        ),
        (
            600,
            FetchUnexpectedClientStatusError,
            {"status_code": 600, "retryable": False},
        ),
    ],
)
def test_external_fetch_error_from_http_status_table(
    status: int,
    exp_cls: type,
    exp_attrs: dict[str, object],
) -> None:
    """status → origin error の対応表を全 row 固定する。"""
    err = external_fetch_error_from_http_status(status, {})
    assert type(err) is exp_cls
    for attr, value in exp_attrs.items():
        assert getattr(err, attr) == value


def test_redirect_does_not_leak_location_header() -> None:
    """3xx 変換は Location header (token を含みうる) を message に載せない。"""
    err = external_fetch_error_from_http_status(
        302, {"location": "http://169.254.169.254/secret?token=abc"}
    )
    assert isinstance(err, FetchRedirectBlockedError)
    assert err.status_code == 302
    assert "169.254.169.254" not in str(err)
    assert str(err) == "fetch_redirect_blocked: HTTP 302"


def test_classify_429_extracts_retry_after() -> None:
    err = external_fetch_error_from_http_status(429, {"Retry-After": "90"})
    assert isinstance(err, FetchRateLimitedError)
    assert err.retry_after_seconds == 90.0


def test_classify_503_extracts_retry_after_but_500_does_not() -> None:
    err503 = external_fetch_error_from_http_status(503, {"retry-after": "30"})
    err500 = external_fetch_error_from_http_status(500, {"Retry-After": "30"})
    assert isinstance(err503, FetchOriginServerError)
    assert err503.retry_after_seconds == 30.0
    assert isinstance(err500, FetchOriginServerError)
    assert err500.retry_after_seconds is None


def test_classify_retry_after_http_date_is_ignored() -> None:
    """HTTP-date 形式の Retry-After は本 stage では parse せず None。"""
    err = external_fetch_error_from_http_status(
        429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
    )
    assert isinstance(err, FetchRateLimitedError)
    assert err.retry_after_seconds is None


def _http_status_error(status: int, headers: dict[str, str]) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://example.com/feed")
    resp = httpx.Response(status, headers=headers, request=req)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


def test_translate_http_status_error_delegates_to_classify() -> None:
    err = external_fetch_error_from_exception(
        _http_status_error(503, {"Retry-After": "120"}),
        target_label="VentureBeat",
    )
    assert isinstance(err, FetchOriginServerError)
    assert err.status_code == 503
    assert err.retry_after_seconds == 120.0


def test_translate_http_status_error_3xx_becomes_redirect_blocked() -> None:
    """例外経路でも 3xx は redirect blocked に倒れ status_code を残す。"""
    err = external_fetch_error_from_exception(
        _http_status_error(302, {"location": "http://169.254.169.254/"}),
        target_label="S",
    )
    assert isinstance(err, FetchRedirectBlockedError)
    assert err.status_code == 302
    assert "169.254.169.254" not in str(err)


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
    err = external_fetch_error_from_exception(exc, target_label="S")
    assert isinstance(err, FetchTimeoutError)


def test_translate_non_timeout_request_error_is_network() -> None:
    err = external_fetch_error_from_exception(
        httpx.ConnectError("connection refused"), target_label="S"
    )
    assert isinstance(err, FetchNetworkError)


def test_translate_host_blocked_is_ssrf() -> None:
    err = external_fetch_error_from_exception(
        HostBlockedError("blocked private ip 10.0.0.1"), target_label="S"
    )
    assert isinstance(err, FetchSsrfBlockedError)


def test_translate_host_resolution_is_network() -> None:
    err = external_fetch_error_from_exception(
        HostResolutionError("dns NXDOMAIN"), target_label="S"
    )
    assert isinstance(err, FetchNetworkError)


def test_translate_unknown_exception_falls_back_to_network() -> None:
    """呼出側 except が広すぎて未知例外が来ても翻訳自体は落ちない。"""
    err = external_fetch_error_from_exception(ValueError("surprise"), target_label="S")
    assert isinstance(err, FetchNetworkError)
