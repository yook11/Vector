"""Fetcher Protocol — per-source 実装の構造的契約。

collection-acquisition-redesign Phase 0c。各ソース毎の Fetcher は
``Fetcher`` Protocol を満たすことだけが要件で、継承関係は持たない。
``runtime_checkable`` は付けない: composition root で静的に組むため
isinstance チェックは不要、かつ ABC 化に伴う MRO 制約も回避する。

各 Fetcher は 1 ソース分の取得結果を
``AsyncIterator[AnalyzableArticle | ObservedArticle]`` で逐次 yield する設計:

- Outcome 純化原則: yield されるのは「次工程に渡す価値のある passport」のみ
- 品質ゲート未達 entry は yield しない (per-entry 失敗は捨てる、観測再導入は
  将来の audit subsystem に委ねる)
- 上流 Service は ``async for item in fetcher.fetch(source_id)`` で受け、
  ``match item`` で ``AnalyzableArticle`` / ``ObservedArticle`` を分岐するだけ
- メモリ効率も良い (RSS feed の全 entry を一括 list 化しない)

Fetcher のアイデンティティは ``NAME`` / ``ENDPOINT_URL`` に内在化されている
(TechCrunch Adapter は **TechCrunch から取る** が不変条件であり、``NewsSource``
ORM から渡される URL ではない)。class attr (per-source Adapter の ClassVar) /
instance attr (``ArticleFetcher`` が Adapter から格上げ) のどちらでも構造的に
満たせるよう ``str`` で緩く受ける。Service / Task は kiq message
に乗せた ``IngestSourceArg(id, name)`` で source_id を Fetcher へ橋渡しし、
Fetcher 自身は ``NewsSource`` を一切知らない。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from app.collection.domain.analyzable_article import AnalyzableArticle
from app.collection.domain.observed_article import ObservedArticle


class Fetcher(Protocol):
    """1 ソース分の取得を担う Fetcher の構造的契約。

    実装は ``async def fetch(self, source_id: int) ->
    AsyncIterator[AnalyzableArticle | ObservedArticle]`` のシグネチャを満たせば
    よく、継承関係は持たない (Protocol による structural subtyping)。RSS / HTML /
    API / クローラなどソース毎の取得方式は実装側に閉じ、上流は出口の
    ``AnalyzableArticle | ObservedArticle`` 型のみに依存する。``source_id`` は
    永続化時の FK 値としてだけ使われ、URL/サイト名は実装側 ClassVar に hardcode
    される。

    ``NAME`` / ``ENDPOINT_URL`` は Fetcher のアイデンティティを内在化する
    属性。``NAME`` は ``FETCHERS`` dispatch dict のキーと一致する文字列
    (= ``news_sources.name`` の StrEnum 値)、``ENDPOINT_URL`` はそのソースの
    feed/API endpoint。``news_sources.endpoint_url`` は historical artifact
    として残置されるが runtime には反映されない。class attr / instance attr
    のいずれでも構造的に満たせるよう ``str`` で宣言する (``ArticleFetcher``
    は Adapter の ClassVar を instance attr に格上げするため)。
    """

    NAME: str
    ENDPOINT_URL: str

    def fetch(
        self, source_id: int
    ) -> AsyncIterator[AnalyzableArticle | ObservedArticle]: ...
