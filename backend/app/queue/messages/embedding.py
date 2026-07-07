"""Stage 5 (Embedding) の kiq message DTO。

`@dataclass(frozen=True, slots=True)` ではなく `BaseModel(frozen=True)` を使う
理由: taskiq の formatter が Pydantic ベースのため、kiq 引数で素の dataclass を
渡すと serializer 到達前に PydanticSerializationError で死ぬ。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class EmbeddingTrigger(BaseModel):
    """Stage 5 起動 trigger — kiq message 用の軽量 ID キャリア。

    precondition は保証せず処理対象の ``analyzed_article_id`` を運ぶ。下流 Stage 5
    Task が ``ReadyForEmbedding.try_advance_from`` を呼んで処理開始時に最新の DB
    状態から Ready を構築する (案 3 = 厚い Ready + 下流 Stage 自身が処理開始時に構築)。

    ``analyzable_article_id`` は監査 (pipeline_events.article_id) の主語となる元記事
    id で、生成元がすでに保持している相関 id。不変 lineage なので trigger 搬送で
    staleness を持ち込まない (可変値の summary / embedding 有無は従来どおり Ready
    構築時に最新 DB から解決する)。移行中は optional で、旧 in-flight message が
    drain したら Phase 2 で required 化する。
    """

    model_config = ConfigDict(frozen=True)

    analyzed_article_id: int = Field(gt=0)
    analyzable_article_id: int | None = Field(default=None, gt=0)
