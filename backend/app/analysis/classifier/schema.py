"""Stage 2 分類 AI レスポンスの Pydantic スキーマ。

AI 境界は常にフラットな ``ClassificationRawResponse`` で受ける（category は
OUT_OF_SCOPE 含む 12 種類から 1 つ選択）。classifier 実装内で ``Classified`` か
``OutOfScope`` のドメイン型に詰め替え、呼び出し側は ``match`` / ``isinstance``
で型ディスパッチする。これにより「分類できたか否か」が型そのもので保証される。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.analysis.domain.value_objects.topic import TopicName


class ValidCategory(StrEnum):
    """AI が出力可能なカテゴリ slug（12 種類）。

    先端技術の 11 カテゴリ + OUT_OF_SCOPE。AI は常にいずれか 1 つを選択する。
    OUT_OF_SCOPE は「上記 11 カテゴリのいずれにも該当しない」を示す 12 番目の
    選択肢であり、Service 層で Rejection 側に振り分ける signal となる。
    """

    AI = "ai"
    BIO = "bio"
    COMPUTING = "computing"
    ENERGY = "energy"
    MATERIALS = "materials"
    MOBILITY = "mobility"
    NETWORK = "network"
    ROBOTICS = "robotics"
    SECURITY = "security"
    SEMICONDUCTOR = "semiconductor"
    SPACE = "space"
    OUT_OF_SCOPE = "out_of_scope"


class ClassificationRawResponse(BaseModel):
    """Gemini SDK の ``response_schema`` に渡す境界型（フラット）。

    AI の出力フォーマットは単一に保ち（精度のため）、ドメイン層への
    詰め替えは classifier 実装内で行う。
    """

    model_config = ConfigDict(frozen=True)

    category: ValidCategory
    topic: TopicName
    topic_label_ja: str = Field(min_length=1, max_length=20)
    reasoning: str = Field(min_length=1)


class Classified(BaseModel):
    """分類に成功したケース（Stage 2 で既存 11 カテゴリのいずれかに分類）。"""

    model_config = ConfigDict(frozen=True)

    category: ValidCategory
    topic: TopicName
    topic_label_ja: str = Field(min_length=1, max_length=20)
    reasoning: str = Field(min_length=1)


class OutOfScope(BaseModel):
    """先端テック領域外、または既存 11 カテゴリに分類不能なケース。"""

    model_config = ConfigDict(frozen=True)

    reasoning: str = Field(min_length=1)


ClassificationResponse = Classified | OutOfScope
"""Stage 2 分類の結果型。Service はこの union を受け取り、``match`` / ``isinstance``
で型ディスパッチする。型そのものが「分類できた／できなかった」を表現する。"""
