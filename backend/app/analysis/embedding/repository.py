"""EmbeddingRepository — Stage 5 埋め込みの永続化。

責務:

- ``try_load_for_embedding``: 「行存在 + 未 embedded + text 取得」を 1 query で
  atomic に判定し、満たす場合のみ ``ReadyForEmbedding`` を直接構築して返す
  (案 3 = 厚い Ready)。Domain 層 ``ReadyForEmbedding.try_advance_from`` は
  本 method への thin delegate。
- ``save``: ``EmbeddingVector`` VO を ``InScopeAssessment`` 行に
  `UPDATE ... WHERE id=:id AND embedding IS NULL RETURNING id` で書き込む。
  楽観ロックで rowcount=1 なら保存成功 (``True``)、rowcount=0 なら並行 update で
  先に書かれていたため自分は保存しなかった (``False``)。読戻しは行わない
  (Service が log + 短絡で抜ける)。

設計方針 (2026-05-12 確定、案 3 + 読戻し廃止): cheap exists 判定と embedder
入力 text fetch を 1 query (``try_load_for_embedding``) に統合した厚い Ready
構造に整合。Repository は「書き込み成否を bool で返すまで」が責務範囲で、
ORM → Entity 復元は持たない。
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.domain.value_objects import EmbeddingVector
from app.models.article_curation import ArticleCuration
from app.models.in_scope_assessment import InScopeAssessment


class EmbeddingRepository:
    """Stage 5 埋め込みの永続化に必要な DB 操作をカプセル化する。

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

        1 query で「行存在 + 未 embedded」を判定し、満たす場合のみ embedder
        入力 (``translated_title`` + ``summary``) と audit 用 ``article_id``
        (``ArticleCuration`` 1-hop JOIN) を取得して厚い Ready を構築して返す。
        行が存在しない / 既 embedded の場合は ``None`` (業務正常)。

        Returns:
            進める場合: precondition (analysis 存在 + 未 embedded) を満たし、
            text + article_id を含む ``ReadyForEmbedding``
            進めない場合: ``None``
        """
        stmt = (
            select(
                InScopeAssessment.translated_title,
                InScopeAssessment.summary,
                ArticleCuration.article_id,
            )
            .join(
                ArticleCuration,
                ArticleCuration.id == InScopeAssessment.curation_id,
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
        translated_title, summary, article_id = row
        return ReadyForEmbedding(
            analysis_id=analysis_id,
            text_for_embedding=f"{translated_title}\n{summary}",
            article_id=article_id,
        )

    async def save(
        self,
        vector: EmbeddingVector,
        *,
        analysis_id: int,
    ) -> bool:
        """``EmbeddingVector`` を ``in_scope_assessments`` 行に UPDATE で永続化する。

        ``WHERE id = :analysis_id AND embedding IS NULL`` の楽観ロックで並行
        save を構造的に解消する。

        embedding カラムのみを更新する。モデル名は ``pipeline_events.payload``
        (audit) を SSoT として記録するため、業務行には焼かない
        (feedback_outcome_purification)。

        commit は呼び出し側 (Service) が行う。

        Returns:
            ``True``: 保存成功 (rowcount=1)
            ``False``: 並行 update で既に書かれていたため保存しなかった
            (rowcount=0、行が既に embedded 済み or 行が存在しない)
        """
        stmt = (
            update(InScopeAssessment)
            .where(
                InScopeAssessment.id == analysis_id,
                InScopeAssessment.embedding.is_(None),
            )
            .values(embedding=vector.to_list())
            .returning(InScopeAssessment.id)
        )
        row = (await self._session.execute(stmt)).first()
        return row is not None
