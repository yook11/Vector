"""``ArticleSource`` — ニュースソースの明示的集約 (P2 の北極星)。

P1 までは「``XxxAdapter`` クラスが per-source 知識 (identity / 補完方針) と
取得実装 (``collect()``) を同居させ、4 共有基底を継承で共有する」暫定構造
だった。P2 は identity / 補完方針を本集約へ集約し、取得実装は ``Source`` が
factory 経由で持つ machinery (``SourceAdapter``) として分離する
(spec §4.1 「1 つの ``Source`` 集約が (a) どう fetch するか=Adapter、
(b) どう完成させるか=``SourceCompletionProfile`` を所有」)。

無 instantiation 契約 (spec §4.6 ガードレール): ``SOURCES`` は本集約の
**インスタンス** を保持するが、``adapter_factory`` は遅延 callable のため
レジストリ構築 (module import) 時に ``RssParser()`` 等の machinery は
構築されない。Stage 2 の profile 解決は ``completion_profile`` フィールドを
直読みするだけで ``make_adapter()`` を呼ばない。よって「profile を読むのに
adapter を作らない」が class-ref ではなく **設計** で担保される。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.collection.domain.observed_article import ObservedOrigin
from app.collection.domain.source_completion_profile import SourceCompletionProfile
from app.collection.fetchers.tools.fetched_article import SourceAdapter
from app.shared.value_objects.source_name import SourceName


@dataclass(frozen=True, slots=True)
class ArticleSource:
    """1 ニュースソース = identity + 補完方針 + 取得 machinery factory。

    - ``name`` / ``endpoint_url``: ソースの identity (``Fetcher`` Protocol が
      要求する ``NAME`` / ``ENDPOINT_URL`` の出所。``name`` は
      ``news_sources.name`` = ``FETCHERS`` dispatch キーと一致する)。
    - ``observed_origin``: 取得チャネル (audit。``ObservedField.origin`` に
      stamp、merge は駆動しない)。
    - ``completion_profile``: 補完方針 (Stage 2 が直読み)。
    - ``adapter_factory``: 取得 machinery の遅延構築 callable。``make_adapter``
      は Stage 1 の fetch 実行時のみ呼ばれる (レジストリ構築時には呼ばない =
      無 instantiation 契約)。
    """

    name: SourceName
    endpoint_url: str
    observed_origin: ObservedOrigin
    completion_profile: SourceCompletionProfile
    adapter_factory: Callable[[], SourceAdapter]

    def make_adapter(self) -> SourceAdapter:
        """取得 machinery を構築する (Stage 1 fetch 実行時のみ呼ぶ)。"""
        return self.adapter_factory()
