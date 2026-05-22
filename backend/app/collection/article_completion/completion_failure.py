"""HTML 完成段で AnalyzableArticle に昇格できなかった理由の閉じ union。

接続/transport 失敗は ``ExternalFetchError`` family、HTML 取得段 (acquisition) の
失敗は ``AcquisitionFailure`` が担う。本モジュールは completion 段のうち
「観測値 + HTML 取得結果を merge して AnalyzableArticle を構築する」段で
起きる失敗だけを扱う。各 variant は失敗地点で得られる証拠 (どの源に値が
あったか / 例外 class+message) を frozen dataclass のフィールドとして
保持し、後段の audit と log emit の双方で構造のまま利用される。

設計は ``acquisition_failure`` と同じ:
- ``reason: ClassVar[str]`` は監査ラベル専用。識別は ``match`` + ``assert_never``
  で型ベースに行う。
- ``__post_init__`` は upper-bound truncate のみ。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, assert_never

_ERROR_MESSAGE_MAX = 500


@dataclass(frozen=True)
class PublishedAtMissing:
    """merge 後も ``published_at`` が埋まらず AnalyzableArticle を完成できなかった。

    ``observed_had_value`` / ``html_had_value`` は merge 入力側で
    ``published_at`` が在ったかの観測点。policy (例: ``html_required``) により
    片方が無視されたケースも、ここでは両源の在/不在をそのまま記録する。
    """

    observed_had_value: bool
    html_had_value: bool
    reason: ClassVar[str] = "published_at_missing"


@dataclass(frozen=True)
class CompletionInvariantRejected:
    """observed/html の値は揃ったが、AnalyzableArticle の Field 制約が完成を拒否した。

    ``AnalyzableArticle`` の不変条件 (title 長 / body 長 / source_id > 0 等) を
    満たさず ``ValueError`` が raise されたケース。例外証拠を保持する。
    """

    error_class: str
    error_message: str
    reason: ClassVar[str] = "invariant_rejected"

    def __post_init__(self) -> None:
        if len(self.error_message) > _ERROR_MESSAGE_MAX:
            object.__setattr__(
                self, "error_message", self.error_message[:_ERROR_MESSAGE_MAX]
            )


ArticleCompletionFailure = PublishedAtMissing | CompletionInvariantRejected
"""HTML 完成段で AnalyzableArticle に昇格できなかった理由の閉じ union (2 variant)。"""


@dataclass(frozen=True, slots=True)
class CompletionRejection:
    """Stage 2 (完成段) のドメイン拒絶。Accept 軸の概念で Retry 軸を持たない。

    完成段の失敗は「再試行で結果が変わるか?」ではなく「ドメイン的に成立するか?」
    の判断であり、acquisition concern の ``Terminal`` | ``Retryable`` とは別の型。
    pending は常に ``closed`` に閉じる (retry は発生しない)。

    ``reason_code`` は ``completion_*`` prefix の audit 集計 key として安定。
    ``detail`` は variant 固有の証拠 (例外 class+message 等) を畳んだ文字列。
    """

    reason_code: str
    detail: str | None = None


def classify_article_completion_failure(
    failure: ArticleCompletionFailure,
) -> CompletionRejection:
    """完成段の失敗を ``completion_*`` prefix の ``CompletionRejection`` に分類する。

    variant 型ベースで dispatch し、各 variant の証拠を ``detail`` に畳む。
    ``reason_code`` は audit 集計 key として安定。
    """
    match failure:
        case PublishedAtMissing():
            return CompletionRejection(reason_code="completion_published_at_missing")
        case CompletionInvariantRejected(error_class=ec, error_message=em):
            return CompletionRejection(
                reason_code="completion_invariant_rejected",
                detail=f"{ec}: {em}",
            )
        case _ as unreachable:
            assert_never(unreachable)
