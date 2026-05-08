"""Stage 4 (Assessment) 判定 AI レスポンスの Pydantic スキーマ。

AI 境界は常にフラットな ``ClassificationRawResponse`` で受ける（category は
OUT_OF_SCOPE 含む 13 種類から 1 つ選択）。classifier 実装内で ``InScope`` か
``OutOfScope`` のドメイン型に詰め替え、呼び出し側は ``match`` / ``isinstance``
で型ディスパッチする。これにより「対象範囲内か否か」が型そのもので保証される。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.analysis.domain.value_objects.topic import TopicName


class ValidCategory(StrEnum):
    """AI が出力可能なカテゴリ slug（13 種類）。

    先端技術の 11 カテゴリ + ``OTHER`` (先端テック領域外で投資判断に寄与する
    記事 — 規制・政策動向・マクロ経済・金融政策・地政学・市場動向・コモディティ等) +
    ``OUT_OF_SCOPE`` (投資判断に寄与しない)。AI は常にいずれか 1 つを選択する。
    ``OUT_OF_SCOPE`` は Service 層で Rejection 側に振り分ける signal となる。
    """

    AI = "ai"
    BIO = "bio"
    COMPUTING = "computing"
    ENERGY = "energy"
    MATERIALS = "materials"
    MOBILITY = "mobility"
    NETWORK = "network"
    OTHER = "other"
    ROBOTICS = "robotics"
    SECURITY = "security"
    SEMICONDUCTOR = "semiconductor"
    SPACE = "space"
    OUT_OF_SCOPE = "out_of_scope"


class ClassificationRawResponse(BaseModel):
    """Gemini SDK の ``response_schema`` に渡す境界型（フラット）。

    AI の出力フォーマットは単一に保ち（精度のため）、ドメイン層への
    詰め替えは classifier 実装内で行う。topic は表示専用属性として降格
    された自由記述ラベル（TopicName VO 正規化済み、最大 3 語）。
    """

    model_config = ConfigDict(frozen=True)

    category: ValidCategory
    topic: TopicName
    investor_take: str = Field(min_length=1)


class InScope(BaseModel):
    """対象範囲内 (in-scope) と判定されたケース。

    Stage 4 で 12 カテゴリ + other のいずれかに分類される。
    """

    model_config = ConfigDict(frozen=True)

    category: ValidCategory
    topic: TopicName
    investor_take: str = Field(min_length=1)


class OutOfScope(BaseModel):
    """対象範囲外 (out-of-scope) — 投資判断に寄与しないと判定されたケース。"""

    model_config = ConfigDict(frozen=True)

    investor_take: str = Field(min_length=1)


AssessmentResponse = InScope | OutOfScope
"""Stage 4 (Assessment) の結果型。Service はこの union を受け取り、
``match`` / ``isinstance`` で型ディスパッチする。型そのものが
「対象範囲内/対象範囲外」を表現する。"""
