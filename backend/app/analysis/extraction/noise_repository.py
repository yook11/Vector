"""Noise リポジトリ — Stage 3 noise 判定された記事の永続化と読み出し。

責務:

- ``exists_for_article``: cheap な exists 判定 (Pattern A' の ``try_advance_from``
  precondition チェック用)
- ``save``: 受け取った ``ExtractionCall[Noise]`` envelope を
  ``INSERT ... ON CONFLICT DO NOTHING RETURNING ...`` で永続化する。entities は
  JSONB カラムにそのまま詰め込み、子テーブル分離は不要。``ON CONFLICT`` の
  target は指定しない (UNIQUE 違反だけでなく ``article_extractions`` 側の
  排他トリガーが fire したケースも吸収するため。
  ``feedback_on_conflict_no_target.md``)。
- ``find_by_article_id``: ORM 行をドメイン Entity (``ExtractionNoise``) として
  復元する (永続化の双対 / race 敗北時の読戻し用)。
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.analysis.domain.value_objects.entity import EntityRawType, EntitySurface
from app.analysis.extraction.ai.envelope import ExtractionCall
from app.analysis.extraction.domain import Noise
from app.analysis.extraction.domain.entity import ExtractedEntity
from app.analysis.extraction.domain.extraction_noise import ExtractionNoise
from app.models.extraction_noise import ExtractionNoise as ExtractionNoiseORM


class NoiseExistenceProtocol(Protocol):
    """Stage 1 進行判定用 NoiseRepository contract (cheap exists 判定)。"""

    async def exists_for_article(self, article_id: int) -> bool: ...


class NoiseRepository:
    """noise 判定された記事の DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists_for_article(self, article_id: int) -> bool:
        """``try_advance_from`` 用 cheap exists 判定 (article_id 単位)。"""
        stmt = (
            select(ExtractionNoiseORM.id)
            .where(ExtractionNoiseORM.article_id == article_id)
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def find_by_article_id(self, article_id: int) -> ExtractionNoise | None:
        """記事に対する既存 noise 記録をドメイン Entity として取得する。"""
        stmt = select(ExtractionNoiseORM).where(
            ExtractionNoiseORM.article_id == article_id
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_domain(orm) if orm is not None else None

    async def save(
        self,
        call: ExtractionCall[Noise],
        *,
        article_id: int,
    ) -> ExtractionNoise | None:
        """``ExtractionCall[Noise]`` を受け、noise 記録を
        ``INSERT ... ON CONFLICT DO NOTHING RETURNING ...`` で永続化する。

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
            (Service が ``find_by_article_id`` で勝者を読み戻す)
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

    @staticmethod
    def _to_domain(orm: ExtractionNoiseORM) -> ExtractionNoise:
        """ORM 行をドメイン Entity に復元する (JSONB → ExtractedEntity tuple)。"""
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
