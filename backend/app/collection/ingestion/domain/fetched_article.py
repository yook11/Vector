"""ingestion BC の出口型 — `FetchedArticle` / `FetchedMetadata` / `FetchOutcome`。

collection-acquisition-redesign Phase 0c + Phase 1.0。各 Fetcher (per-source
実装) が返す出口を以下で固定する:

- ``FetchedArticle`` — 「articles に永続化可能な状態まで揃った」記事の VO。
  品質ゲート (title 非空 / body 50 文字以上 / published_at 必須) と
  source identity (source_id + source_url) を invariant として持つ。
  本クラスは strict 5 フィールドのみ。補足情報は同居しない。
- ``FetchedMetadata`` — capture-everything 用の補足情報 VO。全フィールド
  Optional、ソース毎に埋まり方が異なる (per-source coverage は
  ``Fetcher.PROVIDES`` で静的に宣言)。
- ``FetchOutcome = Ready | Failed`` — 1 entry の処理結果を sum 型で表現。
  ``Ready`` は ``article`` (品質ゲート通過の証明) と ``metadata`` (補足情報)
  の両方を運ぶ。``Failed`` は ``FailureReason`` を伴い、retry 可否と
  分類軸 (``code``) を上流 (Service 層) が一様に扱えるようにする。

Phase 1 以降の各 Fetcher は ``AsyncIterator[FetchOutcome]`` を返し、上流は
``match`` で Ready / Failed を分岐するだけで品質ゲートも source identity も
構造的に保証される (`spec collection-acquisition-redesign.md §3` /
`spec collection-source-data-inventory.md §5`)。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.collection.extraction.domain.value_objects import PublishedAt
from app.shared.value_objects.safe_url import SafeUrl

_TITLE_MIN_LENGTH = 1
_TITLE_MAX_LENGTH = 500
_BODY_MIN_LENGTH = 50
_BODY_MAX_LENGTH = 1_048_576  # 1 MiB

# FetchedMetadata 用 max_length (防御的上限、ソース固有 quirk 由来の暴発防止)
_SUMMARY_MAX_LENGTH = 2000
_AUTHOR_MAX_LENGTH = 200
_LANGUAGE_MAX_LENGTH = 20
_GUID_MAX_LENGTH = 2048
_SITE_NAME_MAX_LENGTH = 100

FailureCode = Literal[
    "http_transient",
    "http_blocked",
    "paywalled",
    "extraction_empty",
    "body_too_short",
    "title_missing",
    "published_at_missing",
    "link_target_failed",
    "other",
]
"""``FetchOutcome.Failed`` の分類軸。

- ``http_transient``: 5xx / 429 / タイムアウト / DNS 一時失敗 (``retryable=True``)
- ``http_blocked``: 403 / 410 / 451 / robots.txt 拒否 (``retryable=False``)
- ``paywalled``: 有料記事と判定された (``retryable=False``)
- ``extraction_empty``: trafilatura パース不能 / Content-Type 不一致
  (``retryable=False``)
- ``body_too_short``: 本文が ``_BODY_MIN_LENGTH`` 未満 (``retryable=False``)
- ``title_missing``: タイトルが空 / 取得不能 (``retryable=False``)
- ``published_at_missing``: 公開日時を確定できなかった (``retryable=False``)
- ``link_target_failed``: HN のような link target に対する派生失敗 (``retryable=False``)
- ``other``: 上記いずれにも該当しない例外的なケース (``retryable=False``)
"""


class FailureReason(BaseModel):
    """``Failed`` が伴う失敗理由。``retryable`` は scheduler の再投入判定で参照する。

    ``detail`` は同 ``code`` を更に細分化する観察用文字列で、メトリクスや構造
    ログで活用する (例: ``code="published_at_missing"`` のとき
    ``detail="rss_pubdate_missing"`` / ``detail="trafilatura_no_date"`` /
    ``detail="date_parse_failed:<raw>"``)。
    """

    model_config = ConfigDict(frozen=True)

    code: FailureCode
    retryable: bool
    detail: str | None = None


class FetchedArticle(BaseModel):
    """articles に永続化可能な状態まで揃った記事の VO。**品質ゲート通過の証明**。

    Invariants (strict、全フィールド必須):
    - ``title``: 非空 1..500 文字 (DB ``original_title`` の ``String(500)`` と一致)
    - ``body``: 50..1_048_576 文字 (``ExtractedContent._BODY_MIN_LENGTH`` 等価)
    - ``published_at``: 必須 (旧 ``ArticleDraft`` の Optional から強化)
    - ``source_id``: 正の整数 (``news_sources.id`` への論理参照)
    - ``source_url``: ``SafeUrl`` (canonical URL、``articles.source_url`` UNIQUE 候補)

    旧 ``ArticleDraft`` との差分は (a) ``source_id`` / ``source_url`` の合成と
    (b) ``published_at`` の必須化。後者は ``published_at_missing`` を Fetcher 側で
    ``Failed`` に分岐させる前提で型レベルで強制する。

    補足情報 (summary / author / tags / image_url 等) は同居せず、姉妹型
    ``FetchedMetadata`` に分離する。本型の契約を「品質保証 = persistence
    可能」に限定するための意図的な分離 (`spec collection-source-data-inventory.md`).
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=_TITLE_MIN_LENGTH, max_length=_TITLE_MAX_LENGTH)
    body: str = Field(min_length=_BODY_MIN_LENGTH, max_length=_BODY_MAX_LENGTH)
    published_at: PublishedAt
    source_id: int = Field(gt=0)
    source_url: SafeUrl


class FetchedMetadata(BaseModel):
    """Fetcher が捕捉した補足情報 (capture-everything)。全フィールド Optional。

    各ソースで埋まり方が異なる (RSS / HTML / API のチャネル + ソース固有の
    availability)。**ソース毎に何を必ず提供するか** は ``Fetcher.PROVIDES``
    で静的に宣言する (per-Fetcher 実装の責務、本クラスは受け皿のみを定義)。

    Tier 設計 (`spec collection-source-data-inventory.md §5`):

    - Tier 1 = 専用フィールドとして昇格 (10 項目、本クラス)
    - Tier 2 = ``extras`` JSONB blob で温存 (将来カラム昇格候補)
    - Tier 3 = Fetcher 内部のみ、本クラスに含めない

    ``extras`` の運用範囲:

    入れて良いもの:
    - 部分カバレッジで将来のカラム昇格候補 (``slash:comments`` /
      ``wp_post_id`` / ``word_count`` / ``is_accessible_for_free`` /
      ``publisher_name`` 等)
    - HN の ``points`` / ``num_comments`` のような source-specific
      永続化候補

    入れてはいけないもの:
    - Tier 1 で受け止めるべき汎用フィールド
    - Fetcher 内部でのみ意味を持つ Tier 3 データ

    ``frozen=True`` は dict の reassignment を防ぐが、ネストした dict の
    mutate は防げない。caller は永続化前に deep-readonly 扱いとする規約。
    """

    model_config = ConfigDict(frozen=True)

    summary: str | None = Field(default=None, max_length=_SUMMARY_MAX_LENGTH)
    author: str | None = Field(default=None, max_length=_AUTHOR_MAX_LENGTH)
    authors: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    image_url: SafeUrl | None = None
    language: str | None = Field(default=None, max_length=_LANGUAGE_MAX_LENGTH)
    guid: str | None = Field(default=None, max_length=_GUID_MAX_LENGTH)
    updated_at: PublishedAt | None = None
    site_name: str | None = Field(default=None, max_length=_SITE_NAME_MAX_LENGTH)
    extras: dict[str, Any] | None = None


class Ready(BaseModel):
    """``FetchOutcome`` の成功側。

    ``article`` が品質ゲート通過の証明、``metadata`` が補足情報。Fetcher は
    article のみ必須で、metadata は省略可能 (空の ``FetchedMetadata()``
    がデフォルト)。
    """

    model_config = ConfigDict(frozen=True)

    article: FetchedArticle
    metadata: FetchedMetadata = Field(default_factory=FetchedMetadata)


class Failed(BaseModel):
    """``FetchOutcome`` の失敗側。理由は ``FailureReason`` で構造化する。"""

    model_config = ConfigDict(frozen=True)

    reason: FailureReason


FetchOutcome = Ready | Failed
"""1 entry の Fetcher 結果。

discriminator tag は持たない単純な Union: in-process でしか流れず taskiq に
直接渡らないため、Pydantic v2 の自動判別で十分 (上流は ``match`` で分岐する)。
"""
