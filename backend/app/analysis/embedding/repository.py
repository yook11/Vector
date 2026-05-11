"""EmbeddingRepository — Stage 5 埋め込みの永続化と読み出し。

責務:

- ``try_load_for_embedding``: 「行存在 + 未 embedded + text 取得」を 1 query で
  atomic に判定し、満たす場合のみ ``ReadyForEmbedding`` を直接構築して返す
  (案 3 = 厚い Ready)。Domain 層 ``ReadyForEmbedding.try_advance_from`` は
  本 method への thin delegate。
- ``save``: ``EmbeddingDraft`` を ``InScopeAssessment`` 行に
  `UPDATE ... WHERE id=:id AND embedding IS NULL RETURNING ...` で書き込む。
  race 敗北時 (rowcount=0) は ``None`` を返し、Service が ``find_by_analysis_id``
  で勝者を読み戻す
- ``find_by_analysis_id``: ORM 行をドメイン Entity (``Embedding``) として復元する。
  ``embedding IS NULL`` のとき ``None`` を返す (ドメイン層で唯一 NULL 判定が
  許される場所)。
- ``_to_domain``: ORM → Entity の内部変換。CHECK 制約と並行する
  defense-in-depth として、片方 NULL の異常状態を ``ValueError`` で即死させる。

設計方針 (2026-05-12 確定、案 3): 旧 Pattern A' (ID-only Ready) 時代に分かれていた
cheap exists 判定と embedder 入力 text fetch を 1 query (``try_load_for_embedding``)
に統合。Ready は **処理に必要な値の全揃え** を構造保証する厚い型として運ばれ、
Repository は Ready 構築に必要な情報を 1 回の DB 往復で完結させる責務を持つ。
"""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.embedding.domain.embedding import Embedding, EmbeddingDraft
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.domain.value_objects import EmbeddingVector
from app.models.in_scope_assessment import InScopeAssessment


class EmbeddingRepository:
    """Stage E 埋め込みの永続化に必要な DB 操作をカプセル化する。

    所有権チェックは呼び出し側の責務。``analysis_id`` は同一 session 内で
    取得した ``InScopeAssessment`` Entity 由来の値を渡すこと (将来 admin re-embed
    endpoint を作る際は別途 authz 設計が必要)。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def try_load_for_embedding(
        self, analysis_id: int
    ) -> ReadyForEmbedding | None:
        """`ReadyForEmbedding.try_advance_from` 用 atomic ロード。

        1 query で「行存在 + 未 embedded」を判定し、満たす場合のみ
        ``translated_title`` + ``summary`` を結合して厚い Ready を構築して返す。
        行が存在しない / 既 embedded の場合は ``None`` (業務正常)。

        Returns:
            進める場合: precondition (analysis 存在 + 未 embedded) を満たし、
            text を含む ``ReadyForEmbedding``
            進めない場合: ``None``
        """
        stmt = (
            select(
                InScopeAssessment.translated_title,
                InScopeAssessment.summary,
            )
            .where(
                InScopeAssessment.id == analysis_id,
                InScopeAssessment.embedding.is_(None),
            )
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        translated_title, summary = row
        return ReadyForEmbedding(
            analysis_id=analysis_id,
            text_for_embedding=f"{translated_title}\n{summary}",
        )

    async def find_by_analysis_id(self, analysis_id: int) -> Embedding | None:
        """analysis に紐づく埋め込みを Entity として取得する (race 敗北時の読戻し用)。

        ``embedding`` カラムが NULL のとき ``None`` を返す。これはドメイン
        モデルの「未生成」状態を行存在 + NULL で表現する設計。
        """
        stmt = select(InScopeAssessment).where(InScopeAssessment.id == analysis_id)
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        if orm is None:
            return None
        return self._to_domain(orm)

    async def save(
        self,
        draft: EmbeddingDraft,
        *,
        analysis_id: int,
        model_name: str,
    ) -> Embedding | None:
        """Draft を ``in_scope_assessments`` 行に条件付き UPDATE で永続化する。

        ``WHERE id = :analysis_id AND embedding IS NULL`` で並行 save レースを
        構造的に解消する。RETURNING で id を受け取り、成功時は draft / 引数値と
        組み合わせて Entity を直接構築する。

        commit は呼び出し側 (Service) が行う。

        Returns:
            成功時: 永続化された ``Embedding`` Entity
            race 敗北時 (rowcount=0): ``None`` (Service が `find_by_analysis_id`
            で勝者を読み戻す)
        """
        stmt = (
            update(InScopeAssessment)
            .where(
                InScopeAssessment.id == analysis_id,
                InScopeAssessment.embedding.is_(None),
            )
            .values(
                embedding=draft.vector.to_list(),
                embedding_model=model_name,
            )
            .returning(InScopeAssessment.id)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return Embedding(
            analysis_id=row.id,
            vector=draft.vector,
            model_name=model_name,
        )

    @staticmethod
    def _to_domain(orm: InScopeAssessment) -> Embedding | None:
        """ORM から記録済み Entity へ復元する。

        ``embedding`` / ``embedding_model`` の整合は CHECK 制約
        ``ck_in_scope_assessments_embedding_consistency`` で構造的に保証される
        が、defense-in-depth として片方 NULL 状態を ``ValueError`` で検知する。
        """
        if orm.embedding is None and orm.embedding_model is None:
            return None
        if orm.embedding is None or orm.embedding_model is None:
            raise ValueError(
                f"InScopeAssessment(id={orm.id}) has inconsistent embedding state: "
                f"embedding={orm.embedding is not None}, "
                f"embedding_model={orm.embedding_model is not None}"
            )
        # HALFVEC カラムは pgvector の HalfVector 型 (リテラル list を渡した場合は
        # そのまま list) として返るため、to_list() があれば呼ぶ。
        raw = orm.embedding
        values = raw.to_list() if hasattr(raw, "to_list") else list(raw)
        return Embedding(
            analysis_id=orm.id,
            vector=EmbeddingVector(root=tuple(values)),
            model_name=orm.embedding_model,
        )
