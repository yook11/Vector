"""``is_infra_scrape_failure`` の分類契約 (正本)。

processing_outcome metric が scrape 失敗を infra_error (一時的、分母外) と failed
(恒久的、分母に算入) に振り分ける述語を固定する。transport は失敗性質の SSoT
``ExternalFetchError.retryable`` に委譲することを、content 失敗は常に failed であり
union が silent に育たないことを検証する。
"""

from __future__ import annotations

from typing import get_args

import pytest

from app.collection.article_completion.outcome import is_infra_scrape_failure
from app.collection.article_completion.scrape_failure import (
    ScrapeContentFailure,
    ScrapeContentQualityTooLow,
    ScrapeNotHtml,
    ScrapeParseCrashed,
    ScrapeParserGaveUp,
)
from app.collection.external_fetch_errors import (
    ExternalFetchError,
    FetchAccessDeniedError,
    FetchNetworkError,
    FetchOriginServerError,
    FetchRateLimitedError,
    FetchResourceNotFoundError,
    FetchRobotsDisallowedError,
    FetchUnexpectedClientStatusError,
    FetchUnexpectedServerStatusError,
)

# 一時的 = retryable transport の代表。述語は性質 SSoT (retryable) を読むので、
# infra/failed を直書きせず instance の retryable と一致することも併せて固定する。
_INFRA_TRANSPORT: tuple[ExternalFetchError, ...] = (
    FetchNetworkError(),
    FetchOriginServerError(status_code=503, reason="service_unavailable"),
    FetchRateLimitedError(),
    FetchUnexpectedServerStatusError(status_code=599),
)
# 恒久的 = non-retryable transport の代表。
_FAILED_TRANSPORT: tuple[ExternalFetchError, ...] = (
    FetchAccessDeniedError(status_code=403, reason="forbidden"),
    FetchResourceNotFoundError(status_code=404, reason="not_found"),
    FetchRobotsDisallowedError(),
    FetchUnexpectedClientStatusError(status_code=418),
)
# 取得できたが使えなかった content 失敗 4 variant (常に failed)。
_CONTENT_FAILURES: tuple[ScrapeContentFailure, ...] = (
    ScrapeNotHtml(content_type="application/pdf"),
    ScrapeParserGaveUp(),
    ScrapeParseCrashed(error_class="ValueError", error_message="boom"),
    ScrapeContentQualityTooLow(body_length=5, title_present=False, body_sample=None),
)


@pytest.mark.parametrize("failure", _INFRA_TRANSPORT)
def test_retryable_transport_is_infra(failure: ExternalFetchError) -> None:
    """一時的 (retryable) な transport 失敗は infra (True)。"""
    assert is_infra_scrape_failure(failure) is True
    assert failure.retryable is True  # 述語が読む SSoT と一致


@pytest.mark.parametrize("failure", _FAILED_TRANSPORT)
def test_terminal_transport_is_not_infra(failure: ExternalFetchError) -> None:
    """恒久的 (non-retryable) な transport 失敗は failed (False)。"""
    assert is_infra_scrape_failure(failure) is False
    assert failure.retryable is False  # 述語が読む SSoT と一致


@pytest.mark.parametrize("failure", _CONTENT_FAILURES)
def test_content_failures_are_not_infra(failure: ScrapeContentFailure) -> None:
    """応答を得たが使えなかった content 失敗は常に failed (False)。"""
    assert is_infra_scrape_failure(failure) is False


def test_content_failure_union_membership_is_pinned() -> None:
    """``ScrapeContentFailure`` の member 集合を固定し、union 拡張を検知する。

    新 content variant を union に足すと本 test が落ち、述語の明示 match +
    ``assert_never`` で分類を載せる判断を強制する (silent な failed 落ち防止)。
    """
    assert set(get_args(ScrapeContentFailure)) == {
        ScrapeNotHtml,
        ScrapeParserGaveUp,
        ScrapeParseCrashed,
        ScrapeContentQualityTooLow,
    }
