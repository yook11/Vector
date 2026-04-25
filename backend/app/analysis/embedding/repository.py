"""EmbeddingRepository — Stage 3 埋め込みの永続化と読み出し。

責務:

- ``save``: ``EmbeddingDraft`` を ``ArticleAnalysis`` 行に書き込む。
  独立 PK と server_default を持たないため戻り値は ``None`` に縮退する
  (PLAN.md §5.1)。Entity の組み立ては呼び出し側 (``Embedding.from_draft``)。
  並行 save レースは ``WHERE embedding IS NULL`` 条件付き UPDATE で構造的に
  解消する (PLAN.md §5.2)。
- ``find_by_analysis_id``: ORM 行をドメイン Entity (``Embedding``) として復元する。
  ``embedding IS NULL`` のとき ``None`` を返す (ドメイン層で唯一 NULL 判定が
  許される場所)。
- ``_to_domain``: ORM → Entity の内部変換。CHECK 制約と並行する
  defense-in-depth として、片方 NULL の異常状態を ``ValueError`` で即死させる
  (PLAN.md §5.3)。
"""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.embedding.domain.embedding import Embedding, EmbeddingDraft
from app.analysis.embedding.domain.value_objects import EmbeddingVector
from app.models.article_analysis import ArticleAnalysis


class EmbeddingRepository:
    """Stage 3 埋め込みの永続化に必要な DB 操作をカプセル化する。

    所有権チェックは呼び出し側の責務。``analysis_id`` は同一 session 内で
    取得した ``Analysis`` Entity 由来の値を渡すこと (将来 admin re-embed
    endpoint を作る際は別途 authz 設計が必要)。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_analysis_id(self, analysis_id: int) -> Embedding | None:
        """analysis に紐づく埋め込みを Entity として取得する (冪等性チェック兼用)。

        ``embedding`` カラムが NULL のとき ``None`` を返す。これはドメイン
        モデルの「未生成」状態を行存在 + NULL で表現する設計 (PLAN.md §3.2)。
        """
        stmt = select(ArticleAnalysis).where(ArticleAnalysis.id == analysis_id)
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
    ) -> bool:
        """Draft を ``article_analyses`` 行に条件付き UPDATE で永続化する。

        ``WHERE id = :analysis_id AND embedding IS NULL`` で並行 save レースを
        構造的に解消する。

        Returns:
            ``True``: 行が更新された (新規埋め込み完了)。
            ``False``: 行不在、または既に他ワーカーが書き込み済み。
                Service 側で ``find_by_analysis_id`` 再取得して
                ``AlreadyEmbeddedOutcome`` 経路に合流させること。

        commit は呼び出し側 (Service) が行う。
        """
        stmt = (
            update(ArticleAnalysis)
            .where(
                ArticleAnalysis.id == analysis_id,
                ArticleAnalysis.embedding.is_(None),
            )
            .values(
                embedding=draft.vector.to_list(),
                embedding_model=model_name,
            )
        )
        result = await self._session.execute(stmt)
        return result.rowcount == 1

    @staticmethod
    def _to_domain(orm: ArticleAnalysis) -> Embedding | None:
        """ORM から記録済み Entity へ復元する。

        ``embedding`` / ``embedding_model`` の整合は CHECK 制約
        ``ck_article_analyses_embedding_consistency`` で構造的に保証される
        が、defense-in-depth として片方 NULL 状態を ``ValueError`` で検知する。
        """
        if orm.embedding is None and orm.embedding_model is None:
            return None
        if orm.embedding is None or orm.embedding_model is None:
            raise ValueError(
                f"ArticleAnalysis(id={orm.id}) has inconsistent embedding state: "
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
