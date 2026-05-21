"""Curation リポジトリ — Stage 3 (signal / noise 両経路) の永続化 + Ready 構築。

Stage 4 ``AssessmentRepository`` (``save_in_scope`` / ``save_out_of_scope`` で
``int | None`` を返す勝者 SSoT パターン) と完全対称に揃え、Stage 3 永続化層を
1 クラス + ``int | None`` 戻り値で表現する。caller は
``CurationRepository(session)`` 1 つだけ instantiate すれば
signal / noise 両 path を扱える。

責務 (Ready 構築):

- ``try_load_for_curation``: 「Article 行存在 + signal/noise 未生成 + 本文サイズ
  ≤ hard cap」を 1 query で atomic に判定し、満たす場合のみ
  ``ReadyForCuration`` を直接構築して返す (案 3 = 厚い Ready)。Domain 層
  ``ReadyForCuration.try_advance_from`` は本 method への thin delegate。
  Stage 4 ``try_load_for_assessment`` と同型。

責務 (signal 経路):

- ``signal_exists_for_article``: cheap な exists 判定 (旧経路 / 別用途で残置、
  Ready 構築経路では ``try_load_for_curation`` 内に集約済)
- ``save_signal``: ``CurationCall[Signal]`` envelope を
  ``INSERT ... ON CONFLICT (article_id) DO NOTHING RETURNING id`` で永続化する。
  race 敗北時 (rowcount=0) は ``None`` を返し、Service が audit / 後続 chain を
  焼かず短絡する (勝者 SSoT、Stage 4 AssessmentRepository と同型)。
- ``update_signal_idempotent``: re-curation CLI 専用 —
  ``CurationCall[Signal]`` のみ受け付け、Noise を update 経路に流す可能性を
  型レベルで排除する (``feedback_structural_guarantee``)。

責務 (noise 経路):

- ``noise_exists_for_article``: cheap な exists 判定 (旧経路 / 別用途で残置、
  Ready 構築経路では ``try_load_for_curation`` 内に集約済)
- ``save_noise``: ``CurationCall[Noise]`` envelope を ``INSERT ... ON
  CONFLICT DO NOTHING RETURNING id`` で永続化する。``ON CONFLICT`` の
  target は指定しない (UNIQUE 違反だけでなく ``article_extractions`` 側の
  排他トリガーが fire したケースも吸収するため。
  ``feedback_on_conflict_no_target.md``)。
"""

from __future__ import annotations

import structlog
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.curation.ai.envelope import CurationCall
from app.analysis.curation.domain import Noise, Signal
from app.analysis.curation.domain.ready import ReadyForCuration
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction
from app.models.extraction_noise import ExtractionNoise as ExtractionNoiseORM

logger = structlog.get_logger(__name__)


class CurationRepository:
    """curation（Stage 3、signal / noise 両経路）に必要な DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Ready 構築 (try_advance_from precondition + curator 入力値)
    # ------------------------------------------------------------------

    async def try_load_for_curation(self, article_id: int) -> ReadyForCuration | None:
        """`ReadyForCuration.try_advance_from` 用 atomic ロード (案 3)。

        1 query で「Article 行存在 + signal/noise 未生成」を判定し、満たす場合
        のみ curator 入力 (title / content) を取得して厚い Ready を構築して返す。
        本文サイズ > ``MAX_CONTENT_LENGTH`` の場合は AI 呼び出し前に枝刈りする
        ため skip log を残して ``None`` を返す (Stage 4 ``try_load_for_assessment``
        と同型の atomic 1 query パターン)。

        Returns:
            進める場合: precondition を満たし、curator 入力値を含む
                ``ReadyForCuration``
            進めない場合: ``None`` (Article 不在 / signal 既存 / noise 既存 /
                本文 oversized)
        """
        stmt = (
            select(Article.original_title, Article.original_content)
            .outerjoin(ArticleExtraction, ArticleExtraction.article_id == Article.id)
            .outerjoin(ExtractionNoiseORM, ExtractionNoiseORM.article_id == Article.id)
            .where(
                Article.id == article_id,
                ArticleExtraction.id.is_(None),
                ExtractionNoiseORM.id.is_(None),
            )
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        title, content = row
        if len(content) > ReadyForCuration.MAX_CONTENT_LENGTH:
            logger.warning(
                "curation_skipped_oversized_article",
                article_id=article_id,
                content_length=len(content),
                max_length=ReadyForCuration.MAX_CONTENT_LENGTH,
            )
            return None
        return ReadyForCuration(
            article_id=article_id,
            original_title=title,
            original_content=content,
        )

    # ------------------------------------------------------------------
    # signal path
    # ------------------------------------------------------------------

    async def signal_exists_for_article(self, article_id: int) -> bool:
        """``try_advance_from`` 用 cheap exists 判定 (signal 側、article_id 単位)。"""
        stmt = (
            select(ArticleExtraction.id)
            .where(ArticleExtraction.article_id == article_id)
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def save_signal(
        self,
        call: CurationCall[Signal],
        *,
        article_id: int,
    ) -> int | None:
        """``CurationCall[Signal]`` を受け、AI 分析結果を
        ``INSERT ... ON CONFLICT (article_id) DO NOTHING RETURNING id`` で
        永続化する。

        ``call.result`` (= ``Signal``) から永続化に必要な値を直接取り出す
        (Stage 3 で起きた事実は envelope が抱え切る、
        ``feedback_bc_boundary_guarantees_downstream``)。
        ``call.model_name`` は監査 SSoT (``pipeline_events.payload.ai_model``)
        に焼くのみで業務行には INSERT しない (``feedback_outcome_purification``)。

        commit は呼び出し側 (Service) が行う。``extracted_at`` は server_default で
        DB が確定させる (本メソッドでは読み戻さない、id のみ返す)。

        Returns:
            成功時: 永続化された ``article_extractions.id`` (``int``)
            race 敗北時 (期待した article_id への UNIQUE 違反): ``None``
            (Service は audit / 後続 chain を焼かず短絡する、勝者 SSoT 同型)
        """
        signal = call.result
        stmt = (
            pg_insert(ArticleExtraction)
            .values(
                article_id=article_id,
                translated_title=signal.title_ja,
                summary=signal.summary_ja,
            )
            .on_conflict_do_nothing(index_elements=["article_id"])
            .returning(ArticleExtraction.id)
        )
        return (await self._session.execute(stmt)).scalar()

    async def update_signal_idempotent(
        self,
        call: CurationCall[Signal],
        *,
        article_id: int,
    ) -> int:
        """既存の Extraction を新しい ``CurationCall[Signal]`` で上書きする (CLI 用)。

        ``CurationCall[Signal]`` のみ受け付ける型 narrow により、Noise を
        signal table に上書きする経路を構造的に排除する
        (``feedback_structural_guarantee``)。

        Phase 1B α-1 の re-curation CLI 専用。再現性を持たせるため:

        - 親 ``ArticleExtraction`` は **UPDATE のみ** (DELETE しない)。これにより
          ``in_scope_assessments`` / ``out_of_scope_assessments`` /
          ``article_embeddings`` / ``watchlist_entries`` への CASCADE 連鎖を
          構造的に回避する
          (parent DELETE するとユーザの watchlist が消失するため)。
        - ``extracted_at`` は ``func.now()`` で再採番する (再抽出した時刻として
          扱い、後段の運用で「いつ抽出された」を取り違えない)。

        対象 article_id に対する extraction が存在しない前提 (CLI 側で事前に
        ``signal_exists_for_article`` で絞り込む)。存在しない場合は
        ``NoResultFound``。

        Returns:
            更新された ``article_extractions.id`` (parent UPDATE のみで id は不変)
        """
        signal = call.result
        update_stmt = (
            update(ArticleExtraction)
            .where(ArticleExtraction.article_id == article_id)
            .values(
                translated_title=signal.title_ja,
                summary=signal.summary_ja,
                extracted_at=func.now(),
            )
            .returning(ArticleExtraction.id)
        )
        extraction_id = (await self._session.execute(update_stmt)).scalar_one()
        await self._session.flush()

        return extraction_id

    # ------------------------------------------------------------------
    # noise path
    # ------------------------------------------------------------------

    async def noise_exists_for_article(self, article_id: int) -> bool:
        """``try_advance_from`` 用 cheap exists 判定 (noise 側、article_id 単位)。"""
        stmt = (
            select(ExtractionNoiseORM.id)
            .where(ExtractionNoiseORM.article_id == article_id)
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def save_noise(
        self,
        call: CurationCall[Noise],
        *,
        article_id: int,
    ) -> int | None:
        """``CurationCall[Noise]`` を受け、noise 記録を ``INSERT ... ON
        CONFLICT DO NOTHING RETURNING id`` で永続化する。

        ``call.result`` (= ``Noise``) から永続化に必要な値を直接取り出す
        (Stage 3 で起きた事実は envelope が抱え切る、
        ``feedback_bc_boundary_guarantees_downstream``)。
        ``call.model_name`` は監査 SSoT (``pipeline_events.payload.ai_model``)
        に焼くのみで業務行には INSERT しない (``feedback_outcome_purification``)。

        commit は呼び出し側 (Service) が行う。``rejected_at`` は server_default
        で DB が確定させる (本メソッドでは読み戻さない、id のみ返す)。

        ``ON CONFLICT`` は target 指定なしで ``DO NOTHING`` を指定する。これに
        より同一 article への UNIQUE 違反だけでなく、排他トリガー
        (``article_extractions`` 側に既に行がある) のケースも同経路で吸収する
        — トリガー fire は ``IntegrityError`` を raise するが、``DO NOTHING`` の
        スコープには入らない。後者は呼び出し側で再 try (taskiq retry) させる。

        Returns:
            成功時: 永続化された ``extraction_noises.id`` (``int``)
            UNIQUE 違反による race 敗北時: ``None``
            (Service は audit / 後続 chain を焼かず短絡する、勝者 SSoT 同型)
        """
        noise = call.result
        stmt = (
            pg_insert(ExtractionNoiseORM)
            .values(
                article_id=article_id,
                title_ja=noise.title_ja,
                summary_ja=noise.summary_ja,
            )
            .on_conflict_do_nothing()
            .returning(ExtractionNoiseORM.id)
        )
        return (await self._session.execute(stmt)).scalar()
