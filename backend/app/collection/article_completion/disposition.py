"""補完失敗を ``CompletionDisposition`` (``Terminal`` | ``Retryable``) に分類する。

``external_fetch_errors.py`` は「何が起きたか」の SSoT で retry / terminal 判断は
持たない。本モジュールが各失敗を必ず分類する。``reason_code`` は監査・log 用の
詳細ラベル、disposition はどう扱うか (close / DB 駆動 retry)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from app.collection.article_completion.completer import (
    CompletionFailure,
    FetchFailed,
)
from app.collection.article_completion.completion_failure import (
    CompletionInvariantRejected,
    PublishedAtMissing,
)
from app.collection.article_completion.extraction_failure import (
    ExtractionCrashed,
    ExtractionFailure,
    NotHtml,
    ParserRejected,
    QualityGateFailed,
)
from app.collection.article_completion.retry_policy import (
    BLIP_POLICY,
    OUTAGE_POLICY,
    RETRY_AFTER_POLICY,
    TIMEOUT_POLICY,
    UNKNOWN_POLICY,
    RetryPolicy,
)
from app.collection.external_fetch_errors import (
    ExternalFetchError,
    FetchAccessDeniedError,
    FetchContentTypeMismatchError,
    FetchGatewayError,
    FetchLegalBlockError,
    FetchNetworkError,
    FetchOriginServerError,
    FetchParseError,
    FetchRateLimitedError,
    FetchRedirectBlockedError,
    FetchRedirectLoopError,
    FetchRequestTimeoutError,
    FetchResourceNotFoundError,
    FetchResponseTooLargeError,
    FetchRetryableStatusError,
    FetchRobotsDisallowedError,
    FetchRobotsUnavailableError,
    FetchSsrfBlockedError,
    FetchTimeoutError,
    FetchUnexpectedStatusError,
)


@dataclass(frozen=True, slots=True)
class Terminal:
    """Stage 2 で再試行しない失敗。pending を ``closed`` に閉じる。"""

    reason_code: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Retryable:
    """DB 駆動 retry する失敗。

    ``policy`` は再投入の仕方を表す純データ。``retry_after_seconds`` は server
    指示があるときだけ載る (なければ ``policy`` の schedule に従う)。
    """

    reason_code: str
    policy: RetryPolicy
    retry_after_seconds: float | None = None
    detail: str | None = None


CompletionDisposition = Terminal | Retryable


# ---------------------------------------------------------------------------
# ExternalFetchError の分類
# ---------------------------------------------------------------------------

# 再試行しても結果が変わらない origin failure。reason_code は exc.CODE を素通し。
_TERMINAL_FETCH_ERROR_TYPES: tuple[type[ExternalFetchError], ...] = (
    FetchAccessDeniedError,
    FetchLegalBlockError,
    FetchResourceNotFoundError,
    FetchSsrfBlockedError,
    FetchRobotsDisallowedError,
    FetchRobotsUnavailableError,
    FetchRedirectBlockedError,
    FetchRedirectLoopError,
    FetchResponseTooLargeError,
    FetchContentTypeMismatchError,
    FetchParseError,
)

# policy ごとに error type を束ねる。同 policy のグループが一目で分かる形。
# ``FetchOriginServerError`` は instance state (reason / retry_after_seconds) を
# 読むため表に入れず ``classify_external_fetch_error`` 内で明示分岐する。
_RETRYABLE_FETCH_ERROR_TYPES_BY_POLICY: tuple[
    tuple[RetryPolicy, tuple[type[ExternalFetchError], ...]],
    ...,
] = (
    (
        BLIP_POLICY,
        (
            FetchGatewayError,
            FetchNetworkError,
        ),
    ),
    (
        TIMEOUT_POLICY,
        (FetchTimeoutError,),
    ),
    (
        UNKNOWN_POLICY,
        (
            FetchRateLimitedError,
            FetchRequestTimeoutError,
            FetchRetryableStatusError,
            FetchUnexpectedStatusError,
        ),
    ),
)

# exact type → disposition の lookup 表。値は frozen dataclass で共有可能。
_FETCH_DISPOSITION_BY_TYPE: dict[type[ExternalFetchError], CompletionDisposition] = {
    **{t: Terminal(reason_code=t.CODE) for t in _TERMINAL_FETCH_ERROR_TYPES},
    **{
        t: Retryable(reason_code=t.CODE, policy=policy)
        for policy, types in _RETRYABLE_FETCH_ERROR_TYPES_BY_POLICY
        for t in types
    },
}


def classify_external_fetch_error(exc: ExternalFetchError) -> CompletionDisposition:
    """origin fetch error を disposition に分類する。

    ``FetchOriginServerError`` は ``reason`` / ``retry_after_seconds`` を読むため
    明示分岐。それ以外は ``type(exc)`` の exact lookup で、未登録のみ保守的に
    ``UNKNOWN_POLICY`` retry。
    """
    if isinstance(exc, FetchOriginServerError):
        if exc.reason == "service_unavailable" and exc.retry_after_seconds is not None:
            return Retryable(
                reason_code=exc.CODE,
                policy=RETRY_AFTER_POLICY,
                retry_after_seconds=exc.retry_after_seconds,
            )
        return Retryable(reason_code=exc.CODE, policy=OUTAGE_POLICY)

    disposition = _FETCH_DISPOSITION_BY_TYPE.get(type(exc))
    if disposition is not None:
        return disposition
    return Retryable(reason_code=exc.CODE, policy=UNKNOWN_POLICY)


# ---------------------------------------------------------------------------
# ExtractionFailure の分類 (全 variant terminal、証拠を detail に畳む)
# ---------------------------------------------------------------------------


def classify_extraction_failure(failure: ExtractionFailure) -> Terminal:
    """HTML 完成段の失敗を terminal に分類し、証拠を ``detail`` に畳む。

    本層は文字列の ``detail`` までで、構造化 audit (``ContentFetchPayload``) への
    転写は別 PR で terminal 経路に recorder を新設して行う。
    """
    detail: str | None
    match failure:
        case NotHtml(content_type=ct):
            detail = f"content_type={ct}"
        case ParserRejected():
            detail = None
        case ExtractionCrashed(stage=s, error_class=ec, error_message=em):
            detail = f"stage={s} {ec}: {em}"
        case QualityGateFailed(body_length=bl, title_present=tp, body_sample=bs):
            sample = f" sample={bs!r}" if bs else ""
            detail = f"body_length={bl} title_present={tp}{sample}"
        case _ as unreachable:
            assert_never(unreachable)
    return Terminal(reason_code=f"extraction_failure_{failure.reason}", detail=detail)


# ---------------------------------------------------------------------------
# ArticleCompletionFailure の分類 (昇格段の domain failure)
# ---------------------------------------------------------------------------


def classify_completion_failed(
    failed: PublishedAtMissing | CompletionInvariantRejected,
) -> Terminal:
    """HTML 補完後の昇格失敗を ``completion_*`` prefix の terminal に分類する。

    variant 型ベースで dispatch し、各 variant の証拠を ``detail`` に畳む。
    ``reason_code`` は audit 集計 key として安定。
    """
    match failed:
        case PublishedAtMissing():
            return Terminal(reason_code="completion_published_at_missing")
        case CompletionInvariantRejected(error_class=ec, error_message=em):
            return Terminal(
                reason_code="completion_invariant_rejected",
                detail=f"{ec}: {em}",
            )
        case _ as unreachable:
            assert_never(unreachable)


# ---------------------------------------------------------------------------
# 閉じ union のディスパッチ (CompletionFailure -> CompletionDisposition)
# ---------------------------------------------------------------------------


def classify_completion_failure(
    failure: CompletionFailure,
) -> CompletionDisposition:
    """``ArticleHtmlCompleter`` が返す閉じ failure union を 1 点で分類する。

    3 classifier (fetch / extraction-failure / promotion) に振り分ける。
    ``ExtractionFailure`` / ``ArticleCompletionFailure`` は union alias で
    クラスパターン非対応のため、variant 型を OR で列挙する。
    """
    match failure:
        case FetchFailed(error=error):
            return classify_external_fetch_error(error)
        case (
            NotHtml() | ParserRejected() | ExtractionCrashed() | QualityGateFailed()
        ) as extraction_failure:
            return classify_extraction_failure(extraction_failure)
        case PublishedAtMissing() | CompletionInvariantRejected() as failed:
            return classify_completion_failed(failed)
        case _ as unreachable:
            assert_never(unreachable)
