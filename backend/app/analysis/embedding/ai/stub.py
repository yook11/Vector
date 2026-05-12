"""決定的な dummy ベクトルを返す Stub Embedder。

CI / Schemathesis 等、外部 API (Gemini) への到達を避けたい環境で使う。
本番経路の Pure DI composition root (``app/brokers.py`` / ``app/search/router.py``
の ``get_embedder_for_search``) は ``GeminiEmbedder`` を hardcode する。
テスト時のみ FastAPI の
``app.dependency_overrides[get_embedder_for_search] = lambda: StubEmbedder()``
で差し替える前提。

セキュリティ / 設計上の不変条件:
- 入力テキストの SHA256 を seed に決定的なベクトルを生成する
  (cassette 録画が安定して比較可能)
- 出力次元は ``GeminiEmbedder.DIMENSION`` と同じ ``768``
  (DB 側 ``HALFVEC(768)`` を壊さない)
- L2 norm = 1.0 に正規化 (cosine distance 比較で挙動再現)
- production 経路には絶対に流入させない。production 用 composition root は
  本クラスを import せず ``GeminiEmbedder`` を hardcode するため、stub 混入は
  型レベルで構造的に不可能。
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import ClassVar

from app.analysis.embedding.ai.base import BaseEmbedder


class StubEmbedder(BaseEmbedder):
    """決定的に dummy ベクトルを返す BaseEmbedder 実装 (CI 専用)。"""

    MODEL: ClassVar[str] = "stub-embedder"
    DIMENSION: ClassVar[int] = 768
    RPM: ClassVar[int | None] = None
    RPD: ClassVar[int | None] = None
    DOCUMENT_PREFIX: ClassVar[str] = ""
    QUERY_PREFIX: ClassVar[str] = ""

    async def _call_api(self, contents: str | list[str]) -> list[list[float]]:
        if isinstance(contents, str):
            return [self._vector_from(contents)]
        return [self._vector_from(t) for t in contents]

    def _translate_error(self, exc: Exception) -> Exception:
        # 例外を起こさないことが Stub の責務。万一発生しても呼び出し側の
        # ``_embed_once`` の網が AnalysisDomainError 階層に丸めるので no-op。
        return exc

    def _vector_from(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # 32 byte digest を float 用に拡張: digest を 192 byte (= 768 / 4) まで
        # 連結して float (4 byte) 単位に切り出す。
        repeats = (self.DIMENSION * 4 + len(digest) - 1) // len(digest)
        buf = (digest * repeats)[: self.DIMENSION * 4]
        raw = [
            struct.unpack("<f", buf[i * 4 : (i + 1) * 4])[0]
            for i in range(self.DIMENSION)
        ]
        # NaN / inf を除去 (digest は任意 byte なので bit 配置が NaN になり得る)。
        cleaned = [0.0 if not math.isfinite(v) else v for v in raw]
        norm = math.sqrt(sum(v * v for v in cleaned))
        if norm == 0.0:
            return [1.0 / math.sqrt(self.DIMENSION)] * self.DIMENSION
        return [v / norm for v in cleaned]
