"""Extraction リポジトリ — Stage 3 (signal / noise 両経路) の永続化。

Stage 4 ``AssessmentRepository`` (``save_in_scope`` / ``save_out_of_scope`` で
``int | None`` を返す勝者 SSoT パターン) と完全対称に揃え、Stage 3 永続化層を
1 クラス + ``int | None`` 戻り値で表現する。caller は
``ExtractionRepository(session)`` 1 つだけ instantiate すれば
signal / noise 両 path を扱える。

責務 (signal 経路):

- ``signal_exists_for_article``: cheap な exists 判定 (Pattern A' の
  ``try_advance_from`` precondition チェック用)
- ``save_signal``: ``ExtractionCall[Signal]`` envelope を
  ``INSERT ... ON CONFLICT (article_id) DO NOTHING RETURNING id`` で永続化する。
  race 敗北時 (rowcount=0) は ``None`` を返し、Service が audit / 後続 chain を
  焼かず短絡する (勝者 SSoT、Stage 4 AssessmentRepository と同型)。子テーブル
  (``article_extraction_entities``) の INSERT は親 INSERT 成功時のみで race
  敗北による orphan を作らない。
- ``update_signal_idempotent``: re-extraction CLI 専用 —
  ``ExtractionCall[Signal]`` のみ受け付け、Noise を update 経路に流す可能性を
  型レベルで排除する (``feedback_structural_guarantee``)。

責務 (noise 経路):

- ``noise_exists_for_article``: cheap な exists 判定 (``try_advance_from``
  precondition チェック用)
- ``save_noise``: ``ExtractionCall[Noise]`` envelope を ``INSERT ... ON
  CONFLICT DO NOTHING RETURNING id`` で永続化する。entities は JSONB
  カラムにそのまま詰め込み、子テーブル分離は不要。``ON CONFLICT`` の
  target は指定しない (UNIQUE 違反だけでなく ``article_extractions`` 側の
  排他トリガーが fire したケースも吸収するため。
  ``feedback_on_conflict_no_target.md``)。
"""

from __future__ import annotations

from sqlalchemy import delete, func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.extraction.ai.envelope import ExtractionCall
from app.analysis.extraction.domain import Noise, Signal
from app.models.article_extraction import ArticleExtraction
from app.models.article_extraction_entity import ArticleExtractionEntity
from app.models.extraction_noise import ExtractionNoise as ExtractionNoiseORM


class ExtractionRepository:
    """事実抽出（Stage 3、signal / noise 両経路）に必要な DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
        call: ExtractionCall[Signal],
        *,
        article_id: int,
    ) -> int | None:
        """``ExtractionCall[Signal]`` を受け、AI 分析結果を
        ``INSERT ... ON CONFLICT (article_id) DO NOTHING RETURNING id`` で
        永続化する。

        ``call.result`` (= ``Signal``) から永続化に必要な値を直接取り出す
        (Stage 3 で起きた事実は envelope が抱え切る、
        ``feedback_bc_boundary_guarantees_downstream``)。
        ``call.model_name`` は監査 SSoT (``pipeline_events.payload.ai_model``)
        に焼くのみで業務行には INSERT しない (``feedback_outcome_purification``)。

        commit は呼び出し側 (Service) が行う。``extracted_at`` は server_default で
        DB が確定させる (本メソッドでは読み戻さない、id のみ返す)。子テーブル
        ``article_extraction_entities`` の INSERT は親 INSERT 成功時のみ実施し、
        race 敗北で orphan エンティティを作らない。

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
        extraction_id = (await self._session.execute(stmt)).scalar()
        if extraction_id is None:
            return None

        # 親 INSERT 成功時のみ子エンティティを INSERT する。``position`` は AI 出力
        # 順を保存し、後段で人間レビュー時に prompt 出力順を再現できるようにする。
        if signal.entities:
            self._session.add_all(
                [
                    ArticleExtractionEntity(
                        extraction_id=extraction_id,
                        surface=e.surface,
                        raw_type=e.raw_type,
                        position=i,
                    )
                    for i, e in enumerate(signal.entities)
                ]
            )
            await self._session.flush()

        return extraction_id

    async def update_signal_idempotent(
        self,
        call: ExtractionCall[Signal],
        *,
        article_id: int,
    ) -> int:
        """既存の Extraction を新しい ``ExtractionCall[Signal]`` で上書きする (CLI 用)。

        ``ExtractionCall[Signal]`` のみ受け付ける型 narrow により、Noise を
        signal table に上書きする経路を構造的に排除する
        (``feedback_structural_guarantee``)。

        Phase 1B α-1 の re-extraction CLI 専用。再現性を持たせるため:

        - 親 ``ArticleExtraction`` は **UPDATE のみ** (DELETE しない)。これにより
          ``in_scope_assessments`` / ``out_of_scope_assessments`` /
          ``article_embeddings`` / ``watchlist_entries`` への CASCADE 連鎖を
          構造的に回避する
          (parent DELETE するとユーザの watchlist が消失するため)。
        - 子 ``article_extraction_entities`` のみ DELETE → INSERT で差し替える
          (新 prompt の出力を新 schema にそのまま流し込む)。
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

        await self._session.execute(
            delete(ArticleExtractionEntity).where(
                ArticleExtractionEntity.extraction_id == extraction_id
            )
        )

        if signal.entities:
            self._session.add_all(
                [
                    ArticleExtractionEntity(
                        extraction_id=extraction_id,
                        surface=e.surface,
                        raw_type=e.raw_type,
                        position=i,
                    )
                    for i, e in enumerate(signal.entities)
                ]
            )
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
        call: ExtractionCall[Noise],
        *,
        article_id: int,
    ) -> int | None:
        """``ExtractionCall[Noise]`` を受け、noise 記録を ``INSERT ... ON
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
        entities_jsonb = [
            {"surface": e.surface.root, "raw_type": e.raw_type.root}
            for e in noise.entities
        ]
        stmt = (
            pg_insert(ExtractionNoiseORM)
            .values(
                article_id=article_id,
                title_ja=noise.title_ja,
                summary_ja=noise.summary_ja,
                entities=entities_jsonb,
            )
            .on_conflict_do_nothing()
            .returning(ExtractionNoiseORM.id)
        )
        return (await self._session.execute(stmt)).scalar()
