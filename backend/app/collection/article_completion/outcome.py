"""scrape 失敗の health-attribution 分類 (metric 集計用)。

``vector.completion.processing_outcome`` が失敗を ``infra_error`` (一時的、成功率の
分母外) と ``failed`` (恒久的、分母に算入) に振り分ける述語。transport は失敗性質の
SSoT である ``ExternalFetchError.retryable`` に委譲し、content 失敗は応答を得たうえで
使えなかった恒久失敗として常に ``failed`` にする。retry / schedule 軸
(``ScrapeTerminal`` / ``ScrapeRetryable``) は handling であり health 軸ではないため
流用しない。
"""

from __future__ import annotations

from typing import assert_never

from app.collection.article_completion.scrape_failure import (
    ScrapeContentQualityTooLow,
    ScrapeFailure,
    ScrapeNotHtml,
    ScrapeParseCrashed,
    ScrapeParserGaveUp,
)
from app.collection.external_fetch_errors import ExternalFetchError


def is_infra_scrape_failure(failure: ScrapeFailure) -> bool:
    """scrape 失敗が infra 起因 (一時的、成功率の分母外) か。

    transport は失敗性質の SSoT (``retryable``) に委譲する。content は閉じ union を
    明示 match し、未分類は ``assert_never`` で型・実行時に落とす (新 variant を
    silent に ``failed`` へ流さない)。
    """
    if isinstance(failure, ExternalFetchError):
        return failure.retryable

    match failure:
        case (
            ScrapeNotHtml()
            | ScrapeParserGaveUp()
            | ScrapeParseCrashed()
            | ScrapeContentQualityTooLow()
        ):
            return False
        case _:
            assert_never(failure)
