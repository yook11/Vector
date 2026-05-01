"""Fetcher Protocol — per-source 実装の構造的契約。

collection-acquisition-redesign Phase 0c。各ソース毎の Fetcher は
``Fetcher`` Protocol を満たすことだけが要件で、継承関係は持たない。
``runtime_checkable`` は付けない: composition root で静的に組むため
isinstance チェックは不要、かつ ABC 化に伴う MRO 制約も回避する。

各 Fetcher は 1 ソース分の取得結果を ``AsyncIterator[FetchOutcome]`` で
逐次 yield する設計:

- 部分回復が型レベルで強制される (1 entry の Failed が他 entry を巻き込まない)
- 上流 Service は ``async for outcome in fetcher.fetch(source)`` で受け、
  ``match outcome`` で ReadyForArticle / PendingHtmlFetch / Failed を分岐するだけ
- メモリ効率も良い (RSS feed の全 entry を一括 list 化しない)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar, Protocol

from app.collection.ingestion.domain.fetched_article import FetchOutcome
from app.models.news_source import NewsSource


class Fetcher(Protocol):
    """1 ソース分の取得を担う Fetcher の構造的契約。

    実装は ``async def fetch(self, source: NewsSource) -> AsyncIterator[FetchOutcome]``
    のシグネチャを満たせばよく、継承関係は持たない (Protocol による structural
    subtyping)。RSS / HTML / API / クローラなどソース毎の取得方式は実装側に閉じ、
    上流は出口の ``FetchOutcome`` 型のみに依存する。

    ``PROVIDES`` はそのソースが ``FetchedMetadata`` の中で **必ず** 値を提供する
    フィールド名の frozenset (Phase 1 の per-source 実装で宣言する)。テスト・
    UI の feature gate・コンポジションルートで「このソースは image_url を必ず
    持つか」を静的に問い合わせるために使う。厳密な runtime 検証 (Fetcher が
    PROVIDES に列挙したフィールドを実際に返したか) は per-source テストで担保
    する。
    """

    PROVIDES: ClassVar[frozenset[str]]

    def fetch(self, source: NewsSource) -> AsyncIterator[FetchOutcome]: ...
