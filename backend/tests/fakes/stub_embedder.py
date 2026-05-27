"""決定的な dummy ベクトルを返す Stub Embedder (Stage 5 document 専用、テスト用)。

CI / Schemathesis 等、外部 API (Gemini) への到達を避けたい環境で使う。
本番経路の Pure DI composition root (``app/brokers.py``) は ``GeminiEmbedder`` を
hardcode しており、本クラスを import しない。Search BC 用 stub は
``tests/fakes/stub_query_embedder.py::StubQueryEmbedder`` に独立する
(BC 分離の徹底、memory `feedback_no_share_different_problems`)。

セキュリティ / 設計上の不変条件:
- 入力テキストの SHA256 を seed に決定的なベクトルを生成する
  (cassette 録画が安定して比較可能)
- 出力次元は ``GeminiEmbedder.dimension`` と同じ ``768``
  (DB 側 ``HALFVEC(768)`` を壊さない)
- L2 norm = 1.0 に正規化 (cosine distance 比較で挙動再現)
- production 経路には絶対に流入させない。production 用 composition root は
  本クラスを import せず ``GeminiEmbedder`` を hardcode するため、stub 混入は
  パッケージ境界 (``tests/`` は production が import しない) で構造的に不可能。
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Final

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.rate_limit import AIModelRateLimitPolicy

_STUB_PROVIDER: Final[str] = "stub"
_STUB_MODEL: Final[str] = "stub-embedder"
_STUB_DIMENSION: Final[int] = 768
_STUB_RATE_LIMIT_POLICY: Final[AIModelRateLimitPolicy] = AIModelRateLimitPolicy(
    provider=_STUB_PROVIDER,
    model=_STUB_MODEL,
    rules=(),
)


class StubEmbedder(BaseEmbedder):
    """決定的に dummy ベクトルを返す BaseEmbedder 実装 (CI 専用)。"""

    @property
    def model_name(self) -> str:
        return _STUB_MODEL

    @property
    def dimension(self) -> int:
        return _STUB_DIMENSION

    @property
    def rate_limit_policy(self) -> AIModelRateLimitPolicy:
        return _STUB_RATE_LIMIT_POLICY

    async def _call_api(self, text: str) -> list[float]:
        return self._vector_from(text)

    def _translate_error(self, exc: Exception) -> Exception:
        # 例外を起こさないことが Stub の責務。万一発生しても caller の
        # ``_embed_once`` が bare re-raise guard (``translated is exc`` 経路)
        # で素通しするため、ここは no-op で exc をそのまま返す。
        return exc

    def _vector_from(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # 32 byte digest を float 用に拡張: digest を 192 byte (= 768 / 4) まで
        # 連結して float (4 byte) 単位に切り出す。
        repeats = (self.dimension * 4 + len(digest) - 1) // len(digest)
        buf = (digest * repeats)[: self.dimension * 4]
        raw = [
            struct.unpack("<f", buf[i * 4 : (i + 1) * 4])[0]
            for i in range(self.dimension)
        ]
        # NaN / inf を除去 (digest は任意 byte なので bit 配置が NaN になり得る)。
        cleaned = [0.0 if not math.isfinite(v) else v for v in raw]
        norm = math.sqrt(sum(v * v for v in cleaned))
        if norm == 0.0:
            return [1.0 / math.sqrt(self.dimension)] * self.dimension
        return [v / norm for v in cleaned]
