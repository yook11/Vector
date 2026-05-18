"""``ArticleSource`` — ニュースソースを 1 クラスで表す構造的契約 (P2-D)。

P1 まで: ``XxxAdapter`` が per-source 知識 (identity / 補完方針) と取得実装
(``collect()``) を同居させ 4 共有基底を継承で共有。
P2(B+C): identity / 補完方針を frozen 集約 ``ArticleSource`` へ移し、取得実装は
``adapter_factory`` 経由の ``SourceAdapter`` machinery として分離 (中間 Adapter
概念が残存)。
P2-D (本実装): Adapter 概念を**除去**。1 ソース = 1 ``XxxSource`` クラスが
identity / 補完方針を ``ClassVar`` で宣言し ``collect(tools)`` で取得手順を
宣言する。**クラスオブジェクトそのもの**が本 Protocol を満たす (registry は
class を値に持つ)。

無 instantiation 契約 (spec §4.6 ガードレール): ``SOURCES`` は Source クラス
オブジェクトを値に持ち、Stage 2 の profile 解決は ``completion_profile`` を
クラス属性として副作用ゼロで読む。``make_adapter()`` / ``adapter_factory`` は
存在しないため「profile を読むのに machinery を作る」経路が**構造的に不能**
(P2 の設計担保より強い class-ref 構造保証)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import SourceCompletionProfile
from app.collection.source_fetch.fetched_article import FetchedArticle
from app.collection.source_fetch.tools.fetch_tools import FetchTools
from app.shared.value_objects.source_name import SourceName


@runtime_checkable
class ArticleSource(Protocol):
    """1 ニュースソース = identity + 補完方針 + 取得手順 (class-level 契約)。

    具体 ``XxxSource`` は ``name`` / ``endpoint_url`` / ``observed_origin`` /
    ``completion_profile`` を ``ClassVar`` 宣言し、``collect`` を
    ``@classmethod`` 実装する。よって**クラスオブジェクト ``XxxSource`` 自体**
    が本 Protocol を構造的に満たす (``XxxSource.collect(tools)`` は bound
    classmethod = 本 Protocol の ``collect(self, tools)`` に一致)。Protocol 側は
    instance 形シグネチャで宣言する (``@classmethod`` は書かない)。

    - ``name`` / ``endpoint_url``: ソース identity (``Fetcher`` Protocol が要求
      する ``NAME`` / ``ENDPOINT_URL`` の出所。``name`` は ``news_sources.name``
      = ``FETCHERS`` dispatch キーと一致)。
    - ``observed_origin``: 取得チャネル (audit。``ObservedField.origin`` に
      stamp、merge は駆動しない)。
    - ``completion_profile``: 補完方針 (Stage 2 が無 instantiation で直読み)。
    - ``collect``: ``FetchTools`` (共通取得道具箱) を使って外部取得し
      ``FetchedArticle`` を逐次 yield する取得手順の宣言。
    """

    name: SourceName
    endpoint_url: str
    observed_origin: ObservedOrigin
    completion_profile: SourceCompletionProfile

    def collect(self, tools: FetchTools) -> AsyncIterator[FetchedArticle]: ...
