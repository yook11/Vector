"""HTML 完成段で AnalyzableArticle に昇格できなかった理由の閉じ union。

接続/transport 失敗は ``ExternalFetchError`` family、HTML 抽出段の失敗は
``ExtractionFailure`` が担う。本モジュールは Stage 2 完成段のうち
「観測値 + HTML 抽出結果を merge して AnalyzableArticle を構築する」段で
起きる失敗だけを扱う。各 variant は失敗地点で得られる証拠 (どの源に値が
あったか / 例外 class+message) を frozen dataclass のフィールドとして
保持し、後段の audit と log emit の双方で構造のまま利用される。

設計は ``extraction_failure`` と同じ:
- ``reason: ClassVar[str]`` は監査ラベル専用。識別は ``match`` + ``assert_never``
  で型ベースに行う。
- ``__post_init__`` は upper-bound truncate のみ。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

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
