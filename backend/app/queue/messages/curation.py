"""Stage 3 (Curation) の kiq message DTO。

`@dataclass(frozen=True, slots=True)` ではなく `BaseModel(frozen=True)` を使う
理由: taskiq の formatter が Pydantic ベースのため、kiq 引数で素の dataclass を
渡すと serializer 到達前に PydanticSerializationError で死ぬ。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CurationTrigger(BaseModel):
    """Stage 3 起動 trigger — kiq message 用の軽量 ID キャリア。

    precondition は保証せず ``article_id`` のみを運ぶ。下流 Stage 3 Task が
    ``ReadyForCuration.try_advance_from`` を呼んで処理開始時に最新の DB 状態から
    Ready を構築する (案 3 = 厚い Ready + 下流 Stage 自身が処理開始時に構築)。

    上流 (collection acquire_source / scrape_html_body / maintenance backfill) は値
    fetch を行わず本 Trigger に ID だけ詰めて kiq に enqueue する。これにより
    kiq message が軽量になり、かつ enqueue → 実行までの時間ずれの影響を受けない
    (Ready 構築時に最新の DB 状態を反映する)。

    Rolling deploy 互換: Pydantic の既定 ``extra='ignore'`` により、旧
    ``ReadyForCuration`` (3 fields: article_id / original_title /
    original_content) の in-flight message を新 worker が受信しても
    ``article_id`` だけ取り出して処理できる。
    """

    model_config = ConfigDict(frozen=True)

    article_id: int = Field(gt=0)
