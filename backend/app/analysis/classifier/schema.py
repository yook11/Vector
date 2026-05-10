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

PR3 で ``ClassificationRawResponse`` (中間 flat 型) を削除した — AI 境界の dict →
ドメイン型詰め替えは ``parse_assessment(payload: dict)`` 1 箇所に集約された。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.analysis.domain.value_objects.topic import TopicName
from app.utils.sanitize import normalize_text


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


class InScope(BaseModel):
    """対象範囲内 (in-scope) と判定された結果。

    ``category`` の型は ``InScopeCategory`` (12 値、``OUT_OF_SCOPE`` 排除) — 「対象
    範囲内」を型そのもので保証する。

    AI 境界として ``investor_take`` を sanitize + 長さ上限で保護する (BC 境界原則:
    feedback_bc_boundary_guarantees_downstream)。下流 (Repository / Entity) は
    再 sanitize しない。
    """

    model_config = ConfigDict(frozen=True)

    category: InScopeCategory
    topic: TopicName
    investor_take: str = Field(min_length=1, max_length=2000)

    @field_validator("investor_take", mode="before")
    @classmethod
    def _sanitize(cls, v: Any) -> Any:
        if isinstance(v, str):
            return normalize_text(v) or ""
        return v

    @field_validator("investor_take")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("investor_take must be non-empty after sanitization")
        return v


class OutOfScope(BaseModel):
    """対象範囲外 (out-of-scope) — 投資判断に寄与しないと判定されたケース。"""

    model_config = ConfigDict(frozen=True)

    investor_take: str = Field(min_length=1, max_length=2000)

    @field_validator("investor_take", mode="before")
    @classmethod
    def _sanitize(cls, v: Any) -> Any:
        if isinstance(v, str):
            return normalize_text(v) or ""
        return v

    @field_validator("investor_take")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("investor_take must be non-empty after sanitization")
        return v


AssessmentResult = InScope | OutOfScope
"""Stage 4 (Assessment) の判定結果。Service はこの union を受け取り
``match`` / ``isinstance`` で型ディスパッチする。型そのものが
「対象範囲内/対象範囲外」を表現する。

旧 ``AssessmentResponse`` から rename (PR2)。
"""
