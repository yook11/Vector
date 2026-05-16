"""Stage 2 (``ArticleCompletionService``) の失敗分類 mapper。

``external_fetch_errors.py`` は「何が起きたか」(origin) の SSoT であり、
retry / terminal 判断は持たない。本モジュールは Stage 2 側の関心として、
各 origin failure を ``CompletionDisposition`` (= ``Terminal`` | ``Retryable``)
に必ず分類する。

2 軸を分離する:

- ``reason_code``: 何が起きたか (監査・log の詳細ラベル)。``Retryable`` にも持たせる。
- disposition: Stage 2 がどう扱うか (terminal close / policy 付き DB 駆動 retry)。

retry policy は ``Retryable`` が運ぶ **データ**。Service 側は policy ごとに
コード分岐せず ``exhausted`` 判定だけで処理経路を 1 本化する。

``classify_external_fetch_error`` は ``type(exc)`` の exact lookup。subclass 追加で
silent fallback しないことは ``test_article_completion_disposition.py`` の網羅
テストが構造的に保証する (分類漏れ = テスト落ち)。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.collection.article_completion.extractor import ExtractionEmpty
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
from app.collection.incomplete_article.domain.completion import ArticleCompletionFailed


@dataclass(frozen=True, slots=True)
class Terminal:
    """Stage 2 で再試行しない失敗。pending を ``closed`` に閉じる。"""

    reason_code: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class Retryable:
    """Stage 2 で DB 駆動 retry する失敗。

    ``policy`` は再投入の仕方を表す純データ。``retry_after_seconds`` は server
    指示があるときだけ載る (なければ ``policy`` の schedule に従う)。
    """

    reason_code: str
    policy: RetryPolicy
    retry_after_seconds: float | None = None
    detail: str | None = None


CompletionDisposition = Terminal | Retryable


# ---------------------------------------------------------------------------
# ExternalFetchError の分類 (spec: ExternalFetchError の分類)
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

# module-load 時に exact type → disposition の dict を生成する。値は frozen
# dataclass なので共有 singleton で安全。
_FETCH_DISPOSITION_BY_TYPE: dict[type[ExternalFetchError], CompletionDisposition] = {
    **{t: Terminal(reason_code=t.CODE) for t in _TERMINAL_FETCH_ERROR_TYPES},
    **{
        t: Retryable(reason_code=t.CODE, policy=policy)
        for policy, types in _RETRYABLE_FETCH_ERROR_TYPES_BY_POLICY
        for t in types
    },
}


def classify_external_fetch_error(exc: ExternalFetchError) -> CompletionDisposition:
    """origin fetch error を Stage 2 の disposition に分類する。

    ``FetchOriginServerError`` は ``reason`` と ``retry_after_seconds`` の
    instance state を読むため明示分岐。それ以外は ``type(exc)`` の exact lookup
    で、未登録 (= 未分類の将来 subclass) のみ保守的に ``UNKNOWN_POLICY`` retry。
    既知 subclass が fallback に落ちないことは網羅テストが保証する。
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
# ExtractionEmpty の分類 (全 reason terminal)
# ---------------------------------------------------------------------------


def classify_extraction_empty(empty: ExtractionEmpty) -> Terminal:
    """「取れたが使える本文でない」結果。3 reason とも terminal に寄せる。"""
    return Terminal(reason_code=f"extraction_empty_{empty.reason}")


# ---------------------------------------------------------------------------
# ArticleCompletionFailed の分類 (domain failure)
# ---------------------------------------------------------------------------


def classify_completion_failed(failed: ArticleCompletionFailed) -> Terminal:
    """HTML 補完後の昇格失敗。``completion_*`` prefix で reason_code 化する。

    domain code は Commit 1 で ``ready_invariant_failed`` に確定済のため、
    ``f"completion_{code}"`` の素直な正規化で過不足ない (旧 ``other`` 残存なし)。
    """
    return Terminal(
        reason_code=f"completion_{failed.reason.code}",
        detail=failed.reason.detail,
    )


# ---------------------------------------------------------------------------
# Persist anomaly の分類 (永続化層の構造異常 — terminal に焼く)
# ---------------------------------------------------------------------------

PERSIST_ANOMALY_REASON_CODE = "article_completion_persist_anomaly"


def classify_persist_anomaly() -> Terminal:
    """``save_ready`` が ``None`` かつ既存 article も読めない構造異常。"""
    return Terminal(reason_code=PERSIST_ANOMALY_REASON_CODE)
