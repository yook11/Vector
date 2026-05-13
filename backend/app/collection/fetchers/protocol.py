"""Fetcher Protocol — per-source 実装の構造的契約。

collection-acquisition-redesign Phase 0c。各ソース毎の Fetcher は
``Fetcher`` Protocol を満たすことだけが要件で、継承関係は持たない。
``runtime_checkable`` は付けない: composition root で静的に組むため
isinstance チェックは不要、かつ ABC 化に伴う MRO 制約も回避する。

各 Fetcher は 1 ソース分の取得結果を ``AsyncIterator[FetchOutcome]`` で
逐次 yield する設計:

- 部分回復が型レベルで強制される (1 entry の Failed が他 entry を巻き込まない)
- 上流 Service は ``async for outcome in fetcher.fetch(source_id)`` で受け、
  ``match outcome`` で ReadyForArticle / IncompleteArticle / Failed を分岐するだけ
- メモリ効率も良い (RSS feed の全 entry を一括 list 化しない)

Fetcher のアイデンティティは ``NAME`` / ``ENDPOINT_URL`` ClassVar に内在化
されている (TechCrunchFetcher は **TechCrunch から取る** が不変条件であり、
``NewsSource`` ORM から渡される URL ではない)。Service / Task は kiq message
に乗せた ``IngestSourceArg(id, name)`` で source_id を Fetcher へ橋渡しし、
Fetcher 自身は ``NewsSource`` を一切知らない。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar, Protocol

from app.collection.ingestion.domain.fetched_article import FetchOutcome


class Fetcher(Protocol):
    """1 ソース分の取得を担う Fetcher の構造的契約。

    実装は ``async def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]``
    のシグネチャを満たせばよく、継承関係は持たない (Protocol による structural
    subtyping)。RSS / HTML / API / クローラなどソース毎の取得方式は実装側に閉じ、
    上流は出口の ``FetchOutcome`` 型のみに依存する。``source_id`` は永続化時の
    FK 値としてだけ使われ、URL/サイト名は実装側 ClassVar に hardcode される。

    ``NAME`` / ``ENDPOINT_URL`` は Fetcher のアイデンティティを内在化する
    ClassVar。``NAME`` は ``FETCHERS`` dispatch dict のキーと一致する文字列
    (= ``news_sources.name`` の StrEnum 値)、``ENDPOINT_URL`` はそのソースの
    feed/API endpoint。``news_sources.endpoint_url`` は historical artifact
    として残置されるが runtime には反映されない。

    ``PROVIDES`` はそのソースが ``FetchedEntry.metadata`` dict に **必ず** key を
    含めるフィールド名の frozenset (Phase 1 の per-source 実装で宣言する)。テスト・
    UI の feature gate・コンポジションルートで「このソースは image_url を必ず
    持つか」を静的に問い合わせるために使う。厳密な runtime 検証 (Fetcher が
    PROVIDES に列挙したフィールドを実際に返したか) は per-source テストで担保
    する。
    """

    NAME: ClassVar[str]
    ENDPOINT_URL: ClassVar[str]
    PROVIDES: ClassVar[frozenset[str]]

    def fetch(self, source_id: int) -> AsyncIterator[FetchOutcome]: ...
