"""Extraction リポジトリ — Stage 3 (signal / noise 両経路) の永続化と読み出し。

Stage 4 ``AssessmentRepository`` (1 クラスで in_scope / out_of_scope 両 path
を扱う) と同型に揃え、Stage 3 永続化層を 1 クラスに集約する。caller は
``ExtractionRepository(session)`` 1 つだけ instantiate すれば
signal/noise 両 path を扱える。

責務 (signal 経路):

- ``signal_exists_for_article``: cheap な exists 判定 (Pattern A' の
  ``try_advance_from`` precondition チェック用)
- ``save_signal``: ``ExtractionCall[Signal]`` envelope を
  ``INSERT ... ON CONFLICT (article_id) DO NOTHING RETURNING ...`` で永続化する。
  race 敗北時 (rowcount=0) は ``None`` を返し、Service が
  ``find_signal_by_article_id`` で勝者を読み戻す (spec §4.6)。子テーブル
  (``article_extraction_entities``) の INSERT は親 INSERT 成功時のみで race
  敗北による orphan を作らない。
- ``find_signal_by_article_id``: ORM 行をドメイン Entity (``Extraction``)
  として復元する (永続化の双対 / race 敗北時の読戻し用)。
- ``update_signal_idempotent``: re-extraction CLI 専用 —
  ``ExtractionCall[Signal]`` のみ受け付け、Noise を update 経路に流す可能性を
  型レベルで排除する (``feedback_structural_guarantee``)。

責務 (noise 経路):

- ``noise_exists_for_article``: cheap な exists 判定 (``try_advance_from``
  precondition チェック用)
- ``save_noise``: ``ExtractionCall[Noise]`` envelope を ``INSERT ... ON
  CONFLICT DO NOTHING RETURNING ...`` で永続化する。entities は JSONB
  カラムにそのまま詰め込み、子テーブル分離は不要。``ON CONFLICT`` の
  target は指定しない (UNIQUE 違反だけでなく ``article_extractions`` 側の
  排他トリガーが fire したケースも吸収するため。
  ``feedback_on_conflict_no_target.md``)。
- ``find_noise_by_article_id``: ORM 行をドメイン Entity (``ExtractionNoise``)
  として復元する (race 敗北時の読戻し用)。
"""

from __future__ import annotations

from sqlalchemy import delete, func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import select

from app.analysis.domain.value_objects.entity import EntityRawType, EntitySurface
from app.analysis.extraction.ai.envelope import ExtractionCall
from app.analysis.extraction.domain import (
    ExtractedEntity,
    Extraction,
    Noise,
    Signal,
)
from app.analysis.extraction.domain.extraction_noise import ExtractionNoise
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

    async def find_signal_by_article_id(self, article_id: int) -> Extraction | None:
        """記事に対する既存 signal 抽出結果をドメイン Entity として取得する。"""
        stmt = (
            select(ArticleExtraction)
            .where(ArticleExtraction.article_id == article_id)
            .options(selectinload(ArticleExtraction.entities))
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._signal_to_domain(orm) if orm is not None else None

    async def save_signal(
        self,
        call: ExtractionCall[Signal],
        *,
        article_id: int,
    ) -> Extraction | None:
        """``ExtractionCall[Signal]`` を受け、AI 分析結果を
        ``INSERT ... ON CONFLICT (article_id) DO NOTHING RETURNING ...`` で
        永続化する。

        ``call.result`` (= ``Signal``) から永続化に必要な値を直接取り出す
        (Stage 3 で起きた事実は envelope が抱え切る、
        ``feedback_bc_boundary_guarantees_downstream``)。
        ``call.model_name`` は監査 SSoT (``pipeline_events.payload.ai_model``)
        に焼くのみで業務行には INSERT しない (``feedback_outcome_purification``)。

        commit は呼び出し側 (Service) が行う。``extracted_at`` は server_default で
        DB が確定させ RETURNING で受け取る。子テーブル
        ``article_extraction_entities`` の INSERT は親 INSERT 成功時のみ実施し、
        race 敗北で orphan エンティティを作らない。

        Returns:
            成功時: 永続化された ``Extraction`` Entity (id / extracted_at は DB 値)
            race 敗北時 (期待した article_id への UNIQUE 違反): ``None``
            (Service が ``find_signal_by_article_id`` で勝者を読み戻す — spec §4.6)
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
            .returning(ArticleExtraction.id, ArticleExtraction.extracted_at)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None

        # 親 INSERT 成功時のみ子エンティティを INSERT する。``position`` は AI 出力
        # 順を保存し、後段で人間レビュー時に prompt 出力順を再現できるようにする。
        if signal.entities:
            self._session.add_all(
                [
                    ArticleExtractionEntity(
                        extraction_id=row.id,
                        surface=e.surface,
                        raw_type=e.raw_type,
                        position=i,
                    )
                    for i, e in enumerate(signal.entities)
                ]
            )
            await self._session.flush()

        return Extraction(
            id=row.id,
            translated_title=signal.title_ja,
            summary=signal.summary_ja,
            entities=tuple(signal.entities),
            extracted_at=row.extracted_at,
        )

    async def update_signal_idempotent(
        self,
        call: ExtractionCall[Signal],
        *,
        article_id: int,
    ) -> Extraction:
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
            .returning(ArticleExtraction.id, ArticleExtraction.extracted_at)
        )
        row = (await self._session.execute(update_stmt)).one()

        await self._session.execute(
            delete(ArticleExtractionEntity).where(
                ArticleExtractionEntity.extraction_id == row.id
            )
        )

        if signal.entities:
            self._session.add_all(
                [
                    ArticleExtractionEntity(
                        extraction_id=row.id,
                        surface=e.surface,
                        raw_type=e.raw_type,
                        position=i,
                    )
                    for i, e in enumerate(signal.entities)
                ]
            )
        await self._session.flush()

        return Extraction(
            id=row.id,
            translated_title=signal.title_ja,
            summary=signal.summary_ja,
            entities=tuple(signal.entities),
            extracted_at=row.extracted_at,
        )

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

    async def find_noise_by_article_id(self, article_id: int) -> ExtractionNoise | None:
        """記事に対する既存 noise 記録をドメイン Entity として取得する。"""
        stmt = select(ExtractionNoiseORM).where(
            ExtractionNoiseORM.article_id == article_id
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._noise_to_domain(orm) if orm is not None else None

    async def save_noise(
        self,
        call: ExtractionCall[Noise],
        *,
        article_id: int,
    ) -> ExtractionNoise | None:
        """``ExtractionCall[Noise]`` を受け、noise 記録を ``INSERT ... ON
        CONFLICT DO NOTHING RETURNING ...`` で永続化する。

        ``call.result`` (= ``Noise``) から永続化に必要な値を直接取り出す
        (Stage 3 で起きた事実は envelope が抱え切る、
        ``feedback_bc_boundary_guarantees_downstream``)。
        ``call.model_name`` は監査 SSoT (``pipeline_events.payload.ai_model``)
        に焼くのみで業務行には INSERT しない (``feedback_outcome_purification``)。

        commit は呼び出し側 (Service) が行う。``rejected_at`` は server_default
        で DB が確定させ RETURNING で受け取る。

        ``ON CONFLICT`` は target 指定なしで ``DO NOTHING`` を指定する。これに
        より同一 article への UNIQUE 違反だけでなく、排他トリガー
        (``article_extractions`` 側に既に行がある) のケースも同経路で吸収する
        — トリガー fire は ``IntegrityError`` を raise するが、``DO NOTHING`` の
        スコープには入らない。後者は呼び出し側で再 try (taskiq retry) させる。

        Returns:
            成功時: 永続化された ``ExtractionNoise`` Entity
            UNIQUE 違反による race 敗北時: ``None``
            (Service が ``find_noise_by_article_id`` で勝者を読み戻す)
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
            .returning(ExtractionNoiseORM.id, ExtractionNoiseORM.rejected_at)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None

        return ExtractionNoise(
            id=row.id,
            article_id=article_id,
            title_ja=noise.title_ja,
            summary_ja=noise.summary_ja,
            entities=tuple(noise.entities),
            rejected_at=row.rejected_at,
        )

    # ------------------------------------------------------------------
    # ORM → domain converters
    # ------------------------------------------------------------------

    @staticmethod
    def _signal_to_domain(orm: ArticleExtraction) -> Extraction:
        """signal 側 ORM から記録済み Entity へ復元する (読み出しの内部処理)。"""
        return Extraction(
            id=orm.id,
            translated_title=orm.translated_title,
            summary=orm.summary,
            entities=tuple(
                ExtractedEntity(surface=e.surface, raw_type=e.raw_type)
                for e in orm.entities
            ),
            extracted_at=orm.extracted_at,
        )

    @staticmethod
    def _noise_to_domain(orm: ExtractionNoiseORM) -> ExtractionNoise:
        """noise 側 ORM 行をドメイン Entity に復元する。

        JSONB の entities 配列を ``ExtractedEntity`` tuple に round-trip 復元する。
        """
        return ExtractionNoise(
            id=orm.id,
            article_id=orm.article_id,
            title_ja=orm.title_ja,
            summary_ja=orm.summary_ja,
            entities=tuple(
                ExtractedEntity(
                    surface=EntitySurface(d["surface"]),
                    raw_type=EntityRawType(d["raw_type"]),
                )
                for d in orm.entities
            ),
            rejected_at=orm.rejected_at,
        )
