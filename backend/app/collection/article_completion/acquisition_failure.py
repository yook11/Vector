"""acquisition concern (Stage 1: Fetch + HTML 抽出) の失敗とその Retry 軸分類。

本モジュールは Stage 1 の失敗を 2 つの面から扱う:

1. 失敗 union (二層): acquire 段の失敗を transport / content の二層で表す。
   - ``ContentFailure`` (4 variant): URL に HTTP GET して HTML を取り、trafilatura で
     本文・タイトル・公開日時を取り出す段で「取得できたが使える本文でなかった」失敗
     (content-type 不一致 / パーサ拒否 / parse 例外 / 品質ゲート未達)。content
     acquisition 層 (parse: ``_parse_raw_response_as_html_document`` / build:
     ``_build_acquired_content_from_document``) はネットワークを持たず構造的にこの
     union しか返せない。各 variant は失敗地点で得られる証拠 (content_type /
     quality metric / 例外 class+message) を frozen dataclass のフィールドに保持する。
   - ``FetchFailed``: 接続 / transport 失敗 (``ExternalFetchError``) を値で畳んだ
     transport variant。``acquire`` の公開境界が内部 ``_fetch`` の raise を捕えて
     値化する。
   - ``AcquisitionFailure = FetchFailed | ContentFailure``: acquire 境界の全失敗。
   後段の audit 記録 (``ContentFetchPayload``) と log emit の双方で構造のまま使う。
2. Retry 軸 disposition: Stage 1 の全失敗 (``AcquisitionFailure``) を ``Terminal`` |
   ``Retryable`` に分類する。Retry 軸は「再試行で結果が変わるか?」の Stage 1 固有概念。
   完成段 (Stage 2 = 抽出物 + メタデータ合成) は別 concern (Accept 軸) として
   ``completion_failure`` の ``CompletionRejection`` で扱う。本モジュールに Stage 2 を
   持ち込まない。

``external_fetch_errors.py`` は「何が起きたか」の SSoT で retry / terminal 判断は
持たない。本モジュールが各失敗を必ず分類する。``reason_code`` は監査・log 用の
詳細ラベル、``AcquisitionDecision`` はどう扱うか (close / DB 駆動 retry)。

設計:
- ``reason: ClassVar[str]`` は監査ラベル専用。識別 (dispatch) は ``match`` +
  ``assert_never`` で型ベースに行う。``FetchFailed`` のみ ``reason`` を持たず、監査
  ラベル (reason_code) は保持する ``exc.CODE`` を素通しする。
- 閾値 (例: ``ARTICLE_BODY_MIN_LENGTH``) は ``article_limits`` SSoT に委譲し、
  本モジュールは生の metric だけを記録する。
- ``__post_init__`` は upper-bound truncate のみ。observation point を壊さない
  ため invariant 違反では raise しない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import ClassVar, Final, assert_never

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

_BODY_SAMPLE_MAX = 200
_ERROR_MESSAGE_MAX = 500
_CONTENT_TYPE_MAX = 200


# ---------------------------------------------------------------------------
# content 失敗 variant (取得できたが使える本文でなかった)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NotHtml:
    """Content-Type が ``text/html`` を含まない。"""

    content_type: str
    reason: ClassVar[str] = "not_html"

    def __post_init__(self) -> None:
        if len(self.content_type) > _CONTENT_TYPE_MAX:
            object.__setattr__(
                self, "content_type", self.content_type[:_CONTENT_TYPE_MAX]
            )


@dataclass(frozen=True)
class ParserGaveUp:
    """``trafilatura.bare_extraction`` が ``None`` — パーサがページ構造化を諦めた。

    例外ではなく正常な戻り値 (``None``) による失敗。「抽出対象がなかった」想定内の
    結果で、経路が壊れた ``ParseCrashed`` (例外) と同一軸 (パーサの振る舞い) の対。
    """

    reason: ClassVar[str] = "parser_gave_up"


@dataclass(frozen=True)
class ParseCrashed:
    """trafilatura (parse) が例外を投げた — 外部ライブラリ経路の故障。

    decode は前工程で input 起因の例外を出さない (charset 不一致は内部で握って
    UTF-8 fallback) ため crash 概念を持たない。本 variant は parse 専用。
    """

    error_class: str
    error_message: str
    reason: ClassVar[str] = "crashed"

    def __post_init__(self) -> None:
        if len(self.error_message) > _ERROR_MESSAGE_MAX:
            object.__setattr__(
                self, "error_message", self.error_message[:_ERROR_MESSAGE_MAX]
            )


@dataclass(frozen=True)
class QualityGateFailed:
    """品質ゲート (本文 50 文字以上 + 非空タイトル) を満たさなかった。

    - ``body_length``: strip 後の実数 (0 含む)。閾値は ``article_limits`` SSoT。
    - ``title_present``: trafilatura が title を返したか (空 / None を ``False``)。
    - ``body_sample``: paywall stub / 拒否ページ判別用の冒頭断片 (≤200 chars)。
      ``None`` のときは本文ゼロまたは閾値以上 (= title 欠落で落ちた) ケース。
    """

    body_length: int
    title_present: bool
    body_sample: str | None
    reason: ClassVar[str] = "quality_gate"

    def __post_init__(self) -> None:
        if self.body_sample is not None and len(self.body_sample) > _BODY_SAMPLE_MAX:
            object.__setattr__(self, "body_sample", self.body_sample[:_BODY_SAMPLE_MAX])


ContentFailure = NotHtml | ParserGaveUp | ParseCrashed | QualityGateFailed
"""取得できたが使える本文でなかった content 失敗を表す閉じ union (4 variant)。

content acquisition 層 (parse: ``_parse_raw_response_as_html_document`` / build:
``_build_acquired_content_from_document``) はネットワークを持たず、構造的にこの union
しか返せない。transport 失敗 (``FetchFailed``) はここに含めない。
"""


# ---------------------------------------------------------------------------
# transport 失敗 variant (接続できなかった)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchFailed:
    """origin fetch が ``ExternalFetchError`` で失敗したことを表す transport variant。

    ``acquire`` の公開境界が内部 ``_fetch`` の raise を捕えて値化する。元の例外は
    ``error`` に保持し、Retry 軸分類 (``classify_external_fetch_error`` に委譲) と log
    で使う。``reason: ClassVar`` は持たない — 監査ラベル (reason_code) は保持する
    ``error.CODE`` を素通しする (``acquisition_{reason}`` 式に乗らない)。
    """

    error: ExternalFetchError


AcquisitionFailure = FetchFailed | ContentFailure
"""acquire 境界の全失敗を表す閉じ union (5 variant)。

transport (``FetchFailed``) + content (``ContentFailure`` の 4 variant)。
"""


# ---------------------------------------------------------------------------
# Retry 軸の処理方針 (decision)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Terminal:
    """Stage 1 (acquisition) で再試行しない終端失敗。pending を ``closed`` に閉じる。"""

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


AcquisitionDecision = Terminal | Retryable
"""Stage 1 (Fetch + HTML 抽出) 失敗の Retry 軸での処理方針 (close / DB 駆動 retry)。"""


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
_RETRYABLE_FETCH_ERROR_TYPES_BY_POLICY: Final[
    Mapping[RetryPolicy, tuple[type[ExternalFetchError], ...]]
] = MappingProxyType(
    {
        BLIP_POLICY: (
            FetchGatewayError,
            FetchNetworkError,
        ),
        TIMEOUT_POLICY: (FetchTimeoutError,),
        UNKNOWN_POLICY: (
            FetchRateLimitedError,
            FetchRequestTimeoutError,
            FetchRetryableStatusError,
            FetchUnexpectedStatusError,
        ),
    }
)

# exact type → decision の lookup 表。値は frozen dataclass で共有可能。
_FETCH_DISPOSITION_BY_TYPE: dict[type[ExternalFetchError], AcquisitionDecision] = {
    **{t: Terminal(reason_code=t.CODE) for t in _TERMINAL_FETCH_ERROR_TYPES},
    **{
        t: Retryable(reason_code=t.CODE, policy=policy)
        for policy, types in _RETRYABLE_FETCH_ERROR_TYPES_BY_POLICY.items()
        for t in types
    },
}


def classify_external_fetch_error(exc: ExternalFetchError) -> AcquisitionDecision:
    """origin fetch error を decision に分類する。

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

    decision = _FETCH_DISPOSITION_BY_TYPE.get(type(exc))
    if decision is not None:
        return decision
    return Retryable(reason_code=exc.CODE, policy=UNKNOWN_POLICY)


# ---------------------------------------------------------------------------
# AcquisitionFailure の分類 (全 variant terminal、証拠を detail に畳む)
# ---------------------------------------------------------------------------


def classify_acquisition_failure(failure: AcquisitionFailure) -> AcquisitionDecision:
    """acquisition 段の失敗を ``AcquisitionDecision`` (Terminal | Retryable) に分類。

    - ``FetchFailed`` (transport): ``classify_external_fetch_error`` に委譲する
      (retryable がありうる)。保持する例外の class+message を ``detail`` に畳む。
    - content 4 種: 常に ``Terminal`` で、証拠を ``detail`` に畳む。

    本層は文字列の ``detail`` までで、構造化 audit (``ContentFetchPayload``) への
    転写は別 PR で terminal 経路に recorder を新設して行う。
    """
    if isinstance(failure, FetchFailed):
        err = failure.error
        return replace(
            classify_external_fetch_error(err),
            detail=f"{type(err).__name__}: {err}",
        )

    detail: str | None
    match failure:
        case NotHtml(content_type=ct):
            detail = f"content_type={ct}"
        case ParserGaveUp():
            detail = None
        case ParseCrashed(error_class=ec, error_message=em):
            detail = f"{ec}: {em}"
        case QualityGateFailed(body_length=bl, title_present=tp, body_sample=bs):
            sample = f" sample={bs!r}" if bs else ""
            detail = f"body_length={bl} title_present={tp}{sample}"
        case _ as unreachable:
            assert_never(unreachable)
    return Terminal(reason_code=f"acquisition_{failure.reason}", detail=detail)
