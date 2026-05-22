"""HTML 完成段で AnalyzableArticle に昇格できなかった理由を表す値。

接続/transport 失敗は ``ExternalFetchError`` family、HTML 取得段 (acquisition) の
失敗は ``AcquisitionFailure`` が担う。本モジュールは completion 段のうち
「観測値 + HTML 取得結果を merge して AnalyzableArticle を構築する」段で
起きる失敗だけを扱う。失敗は構築時の不変条件違反 (published_at 欠落を含む) に
集約され、例外証拠 (class+message) を frozen dataclass のフィールドとして
保持し、後段の audit と log emit の双方で構造のまま利用される。

設計は ``acquisition_failure`` と同じ:
- ``reason: ClassVar[str]`` は監査ラベル専用。
- ``__post_init__`` は upper-bound truncate のみ。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

_ERROR_MESSAGE_MAX = 500


@dataclass(frozen=True)
class CompletionInvariantRejected:
    """merge した値が AnalyzableArticle の Field 制約を満たさず完成を拒否された。

    ``AnalyzableArticle`` の不変条件 (title 長 / body 長 / published_at 必須 /
    source_id > 0 等) を満たさず ``ValueError`` が raise されたケース。
    例外証拠を保持する。
    """

    error_class: str
    error_message: str
    reason: ClassVar[str] = "invariant_rejected"

    def __post_init__(self) -> None:
        if len(self.error_message) > _ERROR_MESSAGE_MAX:
            object.__setattr__(
                self, "error_message", self.error_message[:_ERROR_MESSAGE_MAX]
            )


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
    failure: CompletionInvariantRejected,
) -> CompletionRejection:
    """完成段の失敗を ``completion_*`` prefix の ``CompletionRejection`` に分類する。

    例外証拠 (class+message) を ``detail`` に畳む。``reason_code`` は audit 集計
    key として安定。
    """
    return CompletionRejection(
        reason_code="completion_invariant_rejected",
        detail=f"{failure.error_class}: {failure.error_message}",
    )
