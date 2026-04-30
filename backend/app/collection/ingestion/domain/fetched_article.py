"""ingestion BC の出口型 — `FetchedArticle` と `FetchOutcome`。

collection-acquisition-redesign Phase 0c。各 Fetcher (per-source 実装) が
返す出口を以下 2 つで固定する:

- ``FetchedArticle`` — 「articles に永続化可能な状態まで揃った」記事の VO。
  品質ゲート (title 非空 / body 50 文字以上 / published_at 必須) と
  source identity (source_id + source_url) を invariant として持つ。
- ``FetchOutcome = Ready | Failed`` — 1 entry の処理結果を sum 型で表現。
  ``Failed`` は ``FailureReason`` を伴い、retry 可否と分類軸 (``code``) を
  上流 (Service 層) が一様に扱えるようにする。

Phase 1 以降の各 Fetcher は ``AsyncIterator[FetchOutcome]`` を返し、上流は
``match`` で Ready / Failed を分岐するだけで品質ゲートも source identity も
構造的に保証される (`spec collection-acquisition-redesign.md §3`).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.collection.extraction.domain.value_objects import PublishedAt
from app.shared.value_objects.safe_url import SafeUrl

_TITLE_MIN_LENGTH = 1
_TITLE_MAX_LENGTH = 500
_BODY_MIN_LENGTH = 50
_BODY_MAX_LENGTH = 1_048_576  # 1 MiB

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
    """articles に永続化可能な状態まで揃った記事の VO。

    Invariants:
    - ``title``: 非空 1..500 文字 (DB ``original_title`` の ``String(500)`` と一致)
    - ``body``: 50..1_048_576 文字 (``ExtractedContent._BODY_MIN_LENGTH`` 等価)
    - ``published_at``: 必須 (旧 ``ArticleDraft`` の Optional から強化)
    - ``source_id``: 正の整数 (``news_sources.id`` への論理参照)
    - ``source_url``: ``SafeUrl`` (canonical URL、``articles.source_url`` UNIQUE 候補)

    旧 ``ArticleDraft`` との差分は (a) ``source_id`` / ``source_url`` の合成と
    (b) ``published_at`` の必須化。後者は ``published_at_missing`` を Fetcher 側で
    ``Failed`` に分岐させる前提で型レベルで強制する。
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=_TITLE_MIN_LENGTH, max_length=_TITLE_MAX_LENGTH)
    body: str = Field(min_length=_BODY_MIN_LENGTH, max_length=_BODY_MAX_LENGTH)
    published_at: PublishedAt
    source_id: int = Field(gt=0)
    source_url: SafeUrl


class Ready(BaseModel):
    """``FetchOutcome`` の成功側。``article`` を上流 Service が永続化に渡す。"""

    model_config = ConfigDict(frozen=True)

    article: FetchedArticle


class Failed(BaseModel):
    """``FetchOutcome`` の失敗側。理由は ``FailureReason`` で構造化する。"""

    model_config = ConfigDict(frozen=True)

    reason: FailureReason


FetchOutcome = Ready | Failed
"""1 entry の Fetcher 結果。

discriminator tag は持たない単純な Union: in-process でしか流れず taskiq に
直接渡らないため、Pydantic v2 の自動判別で十分 (上流は ``match`` で分岐する)。
"""
