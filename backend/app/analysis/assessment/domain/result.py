"""Stage 4 (Assessment) のドメイン結果型。

Stage 4 が産出する業務結果 (「対象範囲内 / 対象範囲外」+ taxonomy) を
表す型群。AI が producer であるかどうかに依存しない — AI は domain が
要求する taxonomy に従って返すだけで、これらの型自体は domain 都合で
定義される。

公開型は **「結果型 1 つ + 構成型 3 つ」**:

- ``AssessmentResult = InScope | OutOfScope`` — Stage 4 判定結果 union。
  Service / Repository は ``match`` / ``isinstance`` で型ディスパッチする
- ``InScope`` — 対象範囲内確定後 (``category: InScopeCategory`` で
  OUT_OF_SCOPE を型レベル排除)
- ``OutOfScope`` — 対象範囲外確定後
- ``InScopeCategory`` — in-scope 確定後の category slug 集合 (12 値、
  ``OUT_OF_SCOPE`` 排除)
- ``ValidCategory`` — AI 境界に提示する全 slug 集合 (13 値、
  ``OUT_OF_SCOPE`` を含む domain taxonomy のフラット表現)

AI 境界では引き続きフラット形式 (``{category, investor_take, events}``)
を AI に要求する (構造化出力で discriminated union を要求すると AI 精度が
落ちるため)。assessor 内部 (``ai/parse.py::parse_assessment``) が
``category`` 値を見て ``InScope`` / ``OutOfScope`` に振り分ける。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.shared.text import normalize_text


class ValidCategory(StrEnum):
    """Stage 4 の domain taxonomy 全集合 (13 種、``OUT_OF_SCOPE`` 含む)。

    AI 境界 schema 提示および assessor 内部の parse 検証で使用するが、これは
    AI の都合ではなく domain が要求する taxonomy のフラット表現であり、AI は
    この集合に従って返す責務を負う。判定後は ``InScopeCategory`` に詰め替える
    ため、``InScope`` からは ``OUT_OF_SCOPE`` は型レベルで見えない。

    先端技術の 11 カテゴリ + ``OTHER`` (先端テック領域外で投資判断に寄与する
    記事) + ``OUT_OF_SCOPE`` (投資判断に寄与しない)。
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


class MentionType(StrEnum):
    """event に登場する固有名の役割 6 軸。

    タグ単独で役割が読める命名を優先。company / academic / government の
    3 軸分割は「投資判断視点で役割が違うものを混ぜると集計信号が薄まる」
    という設計判断 (spec §Mention type の 6 軸構成 参照)。
    """

    COMPANY = "company"
    GOVERNMENT = "government"
    ACADEMIC = "academic"
    PRODUCT = "product"
    TECHNOLOGY = "technology"
    PERSON = "person"


class Mention(BaseModel):
    """event に登場した固有名 1 件 (surface + type)。

    AI 境界として ``surface`` を NFKC + 空白整形のみ適用する
    (feedback_ai_extraction_casing: 抽出結果の casing は文脈情報のため
    permissive normalize に留める)。下流 (Repository / Entity) は再 sanitize
    しない (feedback_bc_boundary_guarantees_downstream)。
    """

    model_config = ConfigDict(frozen=True)

    surface: str = Field(min_length=1, max_length=200)
    type: MentionType

    @field_validator("surface", mode="before")
    @classmethod
    def _sanitize(cls, v: Any) -> Any:
        if isinstance(v, str):
            return normalize_text(v) or ""
        return v

    @field_validator("surface")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("surface must be non-empty after sanitization")
        return v


class Event(BaseModel):
    """記事内で起きた事象 1 件 + 登場した固有名のペア。

    ``description`` は「何が起きたか」を表す短文 (字数は AI に厳密指定せず、
    上限のみ structural safety net として置く)。``mentions`` は event に登場した
    固有名のみで、空配列も許容 (spec: 投資判断に直結する mention が無ければ
    空でも可)。
    """

    model_config = ConfigDict(frozen=True)

    description: str = Field(min_length=1, max_length=500)
    mentions: list[Mention] = Field(default_factory=list, max_length=20)

    @field_validator("description", mode="before")
    @classmethod
    def _sanitize(cls, v: Any) -> Any:
        if isinstance(v, str):
            return normalize_text(v) or ""
        return v

    @field_validator("description")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("description must be non-empty after sanitization")
        return v


class InScope(BaseModel):
    """対象範囲内 (in-scope) と判定された結果。

    ``category`` の型は ``InScopeCategory`` (12 値、``OUT_OF_SCOPE`` 排除) — 「対象
    範囲内」を型そのもので保証する。

    AI 境界として ``investor_take`` を sanitize + 長さ上限で保護する (BC 境界原則:
    feedback_bc_boundary_guarantees_downstream)。下流 (Repository / Entity) は
    再 sanitize しない。

    ``events`` は記事内で起きた事象と登場固有名のペア配列で、``default_factory=list``
    で空配列許容 (AI が events を返さない場合の互換性確保)。
    """

    model_config = ConfigDict(frozen=True)

    category: InScopeCategory
    investor_take: str = Field(min_length=1, max_length=2000)
    events: list[Event] = Field(default_factory=list, max_length=10)

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
    """対象範囲外 (out-of-scope) — 投資判断に寄与しないと判定されたケース。

    ``events`` は InScope と対称に保持する (out-of-scope と判定された記事でも
    AI が「何が起きたか」を抽出した結果を検証目的で残す)。``default_factory=list``
    で空配列許容。
    """

    model_config = ConfigDict(frozen=True)

    investor_take: str = Field(min_length=1, max_length=2000)
    events: list[Event] = Field(default_factory=list, max_length=10)

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
"""
