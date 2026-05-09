"""Stage 4 (Assessment) 判定 AI レスポンスの Pydantic スキーマ。

AI 境界では引き続きフラット形式 (``{category, topic, investor_take}``) を AI に
要求する (構造化出力で discriminated union を要求すると AI 精度が落ちるため)。
classifier 内部 (``parse.py::parse_assessment``) が ``category`` 値を見て
``InScope`` / ``OutOfScope`` のドメイン型に振り分け、呼び出し側は
``match`` / ``isinstance`` で型ディスパッチする。

公開型は **「結果型 1 つ + 構成型 2 つ」** に整理:
- ``AssessmentResult = InScope | OutOfScope`` — 判定結果 union
- ``InScope`` (in-scope 確定後、``category: InScopeCategory`` で OUT_OF_SCOPE を型排除)
- ``OutOfScope`` (out-of-scope 確定後)

``ClassificationRawResponse`` (中間 flat 型) は historical な互換性のため PR2 では
保持するが、PR3 で classifier ``_call_api`` の組み替えと同時に削除予定
(spec ``§Classifier 公開型``)。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.analysis.domain.value_objects.topic import TopicName


class ValidCategory(StrEnum):
    """AI が出力可能なカテゴリ slug 全集合 (13 種、``OUT_OF_SCOPE`` 含む)。

    AI への schema 提示および classifier 内部の parse 検証で使用。判定後は
    ``InScopeCategory`` に詰め替えるため、ドメイン側 (``InScope``) からは見えない。
    先端技術の 11 カテゴリ + ``OTHER`` (先端テック領域外で投資判断に寄与する記事) +
    ``OUT_OF_SCOPE`` (投資判断に寄与しない)。
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


class InScopeCategory(StrEnum):
    """in-scope 確定後のカテゴリ slug (12 種)。``OUT_OF_SCOPE`` を型レベルで除外。

    ``InScope.category`` の型に使うことで「対象範囲内なのに OUT_OF_SCOPE」という
    矛盾状態を型システムで排除する。値は ``ValidCategory`` の ``OUT_OF_SCOPE`` 以外
    と完全に一致 (``parse_assessment`` 内で ``InScopeCategory(category.value)`` と
    詰め替える)。

    新値を追加する場合は **必ず ``ValidCategory`` にも追加** すること
    (parse 関数が両者間で値マッピングするため、不一致は ``ValueError`` で検出される)。
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
    # NO OUT_OF_SCOPE — 型レベルで排除


class ClassificationRawResponse(BaseModel):
    """(PR3 で削除予定) Gemini SDK の ``response_schema`` 中間型。

    本クラスは PR3 (classifier ``_call_api`` 改修) で ``parse_assessment`` 経由の
    dict ベース schema 渡しに置き換えられて削除される予定。PR2 では historical
    な caller (``gemini.py`` / ``deepseek.py`` / ``prompts.py::to_domain`` /
    ``gemini_prompt.py``) の互換性のために保持している。新規コードは本型を使わず
    ``parse_assessment(payload: dict)`` を経由すること。
    """

    model_config = ConfigDict(frozen=True)

    category: ValidCategory
    topic: TopicName
    investor_take: str = Field(min_length=1)


class InScope(BaseModel):
    """対象範囲内 (in-scope) と判定された結果。

    ``category`` の型は ``InScopeCategory`` (12 値、``OUT_OF_SCOPE`` 排除) — 「対象
    範囲内」を型そのもので保証する。
    """

    model_config = ConfigDict(frozen=True)

    category: InScopeCategory
    topic: TopicName
    investor_take: str = Field(min_length=1)


class OutOfScope(BaseModel):
    """対象範囲外 (out-of-scope) — 投資判断に寄与しないと判定されたケース。"""

    model_config = ConfigDict(frozen=True)

    investor_take: str = Field(min_length=1)


AssessmentResult = InScope | OutOfScope
"""Stage 4 (Assessment) の判定結果。Service はこの union を受け取り
``match`` / ``isinstance`` で型ディスパッチする。型そのものが
「対象範囲内/対象範囲外」を表現する。

旧 ``AssessmentResponse`` から rename (PR2)。
"""
