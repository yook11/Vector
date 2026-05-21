"""HTML 完成段の失敗を表す閉じ union — 失敗ごとに「どう失敗したか」の証拠を持つ。

接続 / transport 失敗は ``ExternalFetchError`` family (``collection`` 共通) が担う。
本モジュールは Stage 2 完成段 (URL → 本文・タイトル・公開日時) 固有の失敗だけを
扱う。各 variant は失敗地点で得られる証拠 (content_type / parse stage /
quality metric / 例外 class+message) を frozen dataclass のフィールドとして
保持し、後段の audit 記録 (``ContentFetchPayload``) と log emit の双方で
構造のまま利用される。

設計:
- ``reason: ClassVar[str]`` は監査ラベル専用。識別 (dispatch) は ``match`` +
  ``assert_never`` で型ベースに行う。
- 閾値 (例: ``ARTICLE_BODY_MIN_LENGTH``) は ``article_limits`` SSoT に委譲し、
  本モジュールは生の metric だけを記録する。
- ``__post_init__`` は upper-bound truncate のみ。observation point を壊さない
  ため invariant 違反では raise しない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

_BODY_SAMPLE_MAX = 200
_ERROR_MESSAGE_MAX = 500
_CONTENT_TYPE_MAX = 200


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
class ParserRejected:
    """``trafilatura.bare_extraction`` が ``None`` — パーサがページ構造化を放棄。"""

    reason: ClassVar[str] = "parser_rejected"


@dataclass(frozen=True)
class ExtractionCrashed:
    """decode / 抽出処理中に例外。自コード or charset 経路の故障。"""

    stage: Literal["decode", "parse"]
    error_class: str
    error_message: str
    reason: ClassVar[str] = "extraction_crashed"

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


ExtractionFailure = NotHtml | ParserRejected | ExtractionCrashed | QualityGateFailed
"""HTML 完成段の失敗を表す閉じ union (4 variant)。"""
