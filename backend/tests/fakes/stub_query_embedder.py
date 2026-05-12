"""決定的な dummy ベクトルを返す Search BC 専用 Stub Query Embedder (テスト用)。

CI / Schemathesis 等、外部 API (Gemini) への到達を避けたい環境で使う。
本番経路の Pure DI composition root (``app/search/router.py`` の
``get_embedder_for_search``) は ``GeminiQueryEmbedder`` を hardcode しており、
本クラスを import しない。テスト時のみ FastAPI の
``app.dependency_overrides[get_embedder_for_search] = lambda: StubQueryEmbedder()``
で差し替える前提。

Stage 5 の ``StubEmbedder`` (``tests/fakes/stub_embedder.py``) と実装が似ているが、
解いている問題 (Search query 一時計算 vs Stage 5 document 永続化) が違うため
共用しない (memory `feedback_no_share_different_problems`)。
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import ClassVar

from app.search.embedding.base import QueryEmbedder


class StubQueryEmbedder(QueryEmbedder):
    """決定的に dummy ベクトルを返す QueryEmbedder 実装 (CI 専用)。"""

    MODEL: ClassVar[str] = "stub-query-embedder"
    DIMENSION: ClassVar[int] = 768
    QUERY_PREFIX: ClassVar[str] = ""

    async def _call_api(self, text: str) -> list[float]:
        return self._vector_from(text)

    def _translate_error(self, exc: Exception) -> Exception:
        # 例外を起こさないことが Stub の責務。万一発生しても caller の
        # ``_embed_once`` が bare re-raise guard (``translated is exc`` 経路)
        # で素通しするため、ここは no-op で exc をそのまま返す。
        return exc

    def _vector_from(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        repeats = (self.DIMENSION * 4 + len(digest) - 1) // len(digest)
        buf = (digest * repeats)[: self.DIMENSION * 4]
        raw = [
            struct.unpack("<f", buf[i * 4 : (i + 1) * 4])[0]
            for i in range(self.DIMENSION)
        ]
        cleaned = [0.0 if not math.isfinite(v) else v for v in raw]
        norm = math.sqrt(sum(v * v for v in cleaned))
        if norm == 0.0:
            return [1.0 / math.sqrt(self.DIMENSION)] * self.DIMENSION
        return [v / norm for v in cleaned]
