"""Entity — 抽出された固有名の複合 VO。

``EntityName`` と ``EntityType`` を束ねた値オブジェクト。``ExtractionResult``
(AI 応答) と ``Extraction`` (記録済み Entity) の双方で部品として使われる。

Pydantic BaseModel で実装することで、Gemini の ``response_schema`` に
ネストして流せる (AI 境界契約) と同時に、ドメインメソッド (dedup_key) を
持たせられる。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.analysis.domain.value_objects.entity import EntityName, EntityType


class Entity(BaseModel):
    """AI が抽出したエンティティ 1 件の複合 VO。

    Invariants:
    - name / type は各 VO の不変条件を満たす
    - frozen: 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    name: EntityName
    type: EntityType

    def dedup_key(self) -> tuple[str, str]:
        """同一エンティティ判定キー。

        ``EntityName`` は表示用に大文字小文字を保持するが、重複判定では
        無視する ("NVIDIA" と "nvidia" は同一エンティティ)。``EntityType``
        は VO 側で小文字正規化済み。
        """
        return (self.name.root.casefold(), self.type.root)
