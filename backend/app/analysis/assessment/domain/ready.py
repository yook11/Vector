"""ReadyForAssessment — Stage 4 実行可能状態の precondition 型 (案 3: 厚い Ready)。

Stage 4 BC 実装。Stage 4 operation の前提条件 (Extraction 存在 +
InScopeAssessment 未生成 + OutOfScopeAssessment 未生成) を構造保証し、かつ
assessor 入力 text + audit に必要な参照値も含めて運ぶ厚い Ready。

設計方針 (2026-05-12 確定、案 3): Ready 型は **処理に必要な値の全揃え** を構造保証する
厚い型であり、**下流 Stage 自身 (Stage 4 Task) が処理開始時に DB から内容を fetch
して構築** する。上流 Stage 3 Task から Stage 4 への kiq message は ID のみ運ぶ
``AssessmentTrigger`` を用い、Stage 4 Task が ``Ready.try_advance_from`` を呼んで
最新の DB 状態から Ready を構築する。

旧 Pattern A' (ID-only Ready + 上流 Task 構築 + AuditRepository 2-hop 逆引き) は
以下 3 点で構造保証の実体が弱かったため撤回:

1. 値弱の Ready (3 fields) で audit に必要な ``article_id`` / ``source_name`` を
   AuditRepository が DB 逆引きで再構築 → BC 境界の責務が漏出
2. kiq enqueue → 実行までに DB 状態が変わるため、上流 Task で構築した Ready は
   precondition の時間ずれを許容し、Service が暗黙的に再検証
3. 「下流で次に進むことを上流が保証する」設計で責務の主語が間違っている

詳細は memory `project_typed_pipeline_preconditions.md` (2026-05-11 確定版) と
spec `specs/backlog/stage4-ready-thick-pattern.md`。

`@dataclass(frozen=True, slots=True)` ではなく `BaseModel(frozen=True)` を使う
理由: taskiq の formatter が Pydantic ベースのため、kiq 引数で素の dataclass を
渡すと serializer 到達前に PydanticSerializationError で死ぬ (taskiq Issue #441)。
詳細は memory `feedback_taskiq_basemodel_required.md`。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class AssessmentPreconditionProtocol(Protocol):
    """Stage 4 進行判定用 Assessment Repository contract。

    「Ready 構築に必要なデータをロードする」= 「ReadyForAssessment を満たす」
    という意味論で、Repository は precondition を満たす場合に
    ``ReadyForAssessment`` を atomic な 1 query で構築して返す責務を持つ。
    `try_advance_from` は本 Protocol への thin delegate。
    """

    async def try_load_for_assessment(
        self, curation_id: int
    ) -> ReadyForAssessment | None: ...


class ReadyForAssessment(BaseModel):
    """Stage 4 assessment を実行可能な状態を表す precondition 型 (厚い Ready)。

    フィールドは operation を特定する ID と、assessor 入力 + audit 用参照値の全揃え。
    Ready が存在する = 処理開始時点で DB から値を取得済 + 行存在 + 両 assessment
    未生成 が verify された状態 (時間ずれゼロ)。Service / AuditRepository は
    ``ready`` から直接値を取り、自身で DB 逆引きを行わない。

    Invariants:
    - ``curation_id``: 正の整数 (DB の ArticleCuration.id を指す)
    - ``translated_title`` / ``summary``: Stage 3 で確定済の本文 (assessor 入力)
    - ``article_id``: 正の整数 (audit の ``pipeline_events.article_id`` 列に詰める)
    - ``source_name``: NewsSource.name (audit payload)、FK 切断時は None
    - frozen: 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    curation_id: int = Field(gt=0)
    translated_title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    article_id: int = Field(gt=0)
    source_name: str | None = None

    @classmethod
    async def try_advance_from(
        cls,
        *,
        curation_id: int,
        repo: AssessmentPreconditionProtocol,
    ) -> ReadyForAssessment | None:
        """curation_id から Stage 4 へ advance できるかを判定する gatekeeper。

        Precondition (Stage 4 に進める条件):
        - 同 curation_id の ArticleCuration 行が存在
        - 同 curation_id の InScopeAssessment 未生成
        - 同 curation_id の OutOfScopeAssessment 未生成

        本 method は Domain 層の named gateway として
        `Repository.try_load_for_assessment` にそのまま delegate する。
        Repository が atomic な 1 query で precondition 判定 + 厚い Ready の
        構築を完結させる。

        Returns:
            進める場合: `ReadyForAssessment` (audit 参照値も含む厚い型)
            進めない場合: `None` (業務正常状態、例外ではない)

        Args:
            curation_id: 上流 Stage 3 で永続化された ArticleCuration.id
            repo: ``try_load_for_assessment`` を備える Repository
        """
        return await repo.try_load_for_assessment(curation_id)


class AssessmentTrigger(BaseModel):
    """Stage 4 起動 trigger — kiq message 用の軽量 ID キャリア。

    precondition は保証せず ``curation_id`` のみを運ぶ。下流 Stage 4 Task が
    ``ReadyForAssessment.try_advance_from`` を呼んで処理開始時に最新の DB 状態から
    Ready を構築する (案 3 = 厚い Ready + 下流 Stage 自身が処理開始時に構築)。

    上流 (Stage 3 Task / maintenance backfill) は値 fetch を行わず本 Trigger に
    ID だけ詰めて kiq に enqueue する。これにより kiq message が軽量になり、
    かつ enqueue → 実行までの時間ずれの影響を受けない (Ready 構築時に最新の
    DB 状態を反映する)。

    taskiq formatter は Pydantic BaseModel(frozen=True) を要求するため
    ``BaseModel`` 派生 (memory `feedback_taskiq_basemodel_required.md`)。
    """

    model_config = ConfigDict(frozen=True)

    curation_id: int = Field(gt=0)
