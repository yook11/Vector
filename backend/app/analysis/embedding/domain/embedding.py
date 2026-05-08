"""Embedding アグリゲート — Stage E で生成された埋め込みベクトル。

2 つの型で Stage E の概念を表す:

- ``EmbeddingDraft`` — AI 境界の ``list[float]`` を ``EmbeddingVector`` VO に
  正規化したドメイン入力。永続化前の状態で、analysis_id / model_name は
  Service が解決する (``Repository.save`` の引数で受ける)。
- ``Embedding`` — システムに記録された埋め込み Entity。identity は
  ``analysis_id`` (DB 同一行ゆえの妥協、別テーブル化時に独立 PK が出る前提)。

変換は ``EmbeddingDraft.from_inference`` (AI 境界 → Draft) と Repository.save
(Draft + identity → Entity)、Repository._to_domain (ORM → Entity) が担う。
Pattern A' (typed-pipeline-preconditions.md §8) で ``Embedding.from_draft``
ファクトリは廃止された (Repository.save が直接 Entity を返すため Service 内
での組み立て不要)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from pydantic import BaseModel, ConfigDict

from app.analysis.embedding.domain.value_objects import EmbeddingVector

_MODEL_NAME_MAX_LENGTH = 100


class EmbeddingDraft(BaseModel):
    """埋め込み生成結果のドメイン入力 (AI 出力 → 永続化前の正規化値)。

    AI 境界の ``list[float]`` を ``EmbeddingVector`` VO に正規化した状態。
    analysis_id / model_name はこの段階では未確定で、Service が
    ``Repository.save`` 呼び出し時に注入する。

    Invariants:
    - ``vector``: ``EmbeddingVector`` (768 dim + 有限性 + サニティ範囲)
    - frozen: 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    vector: EmbeddingVector

    @classmethod
    def from_inference(cls, *, vector: list[float]) -> Self:
        """AI 境界の ``list[float]`` を Draft に正規化する。

        embedder の戻り値をそのまま受け取り、``EmbeddingVector`` で
        次元・有限性・サニティ範囲を構造的に検証する純粋変換。
        """
        return cls(vector=EmbeddingVector(root=tuple(vector)))


@dataclass(frozen=True, slots=True)
class Embedding:
    """システムに記録された埋め込み Entity。

    Stage 5 (Embedding) の成果物。identity は ``analysis_id`` で、これは
    ``InScopeAssessment.id`` と同一 (現状は同一行に embedding / embedding_model
    カラムを持つ DB 物理事実ゆえの妥協)。別テーブル化時には独立 PK を
    導入する余地を残すが、ドメイン層ではすでに「Embedding aggregate」として
    InScopeAssessment から分離して扱う。

    注 (PR3.5-d.0): field 名 ``analysis_id`` は taskiq in-flight message 互換と
    embedding stage rename の対象外につき据え置き。embedding 側 rename は
    別 PR で扱う。

    ``generated_at`` は意図的に持たない (PLAN.md §3.4)。
    現状の表示要件がなく、``Optional[datetime]`` は構造的保証を弱めるため、
    必要になった時点で別テーブル化と合わせて再評価する。

    Invariants:
    - ``analysis_id`` は正の整数
    - ``vector`` は ``EmbeddingVector``
    - ``model_name`` は非空 1..100 文字
    """

    analysis_id: int
    vector: EmbeddingVector
    model_name: str

    def __post_init__(self) -> None:
        if self.analysis_id <= 0:
            raise ValueError("Embedding.analysis_id must be positive")
        if not self.model_name:
            raise ValueError("Embedding.model_name must be non-empty")
        if len(self.model_name) > _MODEL_NAME_MAX_LENGTH:
            raise ValueError(
                f"Embedding.model_name must be at most {_MODEL_NAME_MAX_LENGTH} chars, "
                f"got {len(self.model_name)}"
            )
