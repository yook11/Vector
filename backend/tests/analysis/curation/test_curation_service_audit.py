"""``CurationService`` の成功経路で audit が焼付けられる integration test。

PR1-c で Outcome を廃止し戻り値を ``int | None`` 一本化したため、本 file は
Outcome 型 assertion を「signal 勝者 → ``int``、noise 勝者 → ``None``」に
書き換えている。

検証する性質:
- signal 勝者 → ``outcome_code='curated_signal'`` (SUCCEEDED) + ``category='success'`` +
  ``code='curated_signal'``、Service は ``curation_id`` (``int``) を返す
- noise 勝者 → ``outcome_code='curated_noise'`` (SUCCEEDED) +
  ``category='success'`` + ``code='curated_noise'``、Service は ``None`` を返す
  (Stage 4 chain しない、Task 層は ``if result is None: return`` で短絡)
- ``CurationResponseInvalidError`` (Layer 2-B) は Service が catch せず
  そのまま raise される (audit は task 層が焼く責務)
- 各 audit row に ``ai_model`` / ``prompt_version`` / ``input_content_*`` /
  ``source_name`` が payload に焼かれている
- 成功系では ``ai_raw_response`` も焼かれる
- ``article_id`` / ``source_id`` (auto-resolve) が両方埋まる
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.ai.envelope import CurationCall
from app.analysis.curation.ai.gemini_spec import GEMINI_CURATION_SPEC
from app.analysis.curation.domain import Noise, Signal
from app.analysis.curation.domain.ready import ReadyForCuration
from app.analysis.curation.errors import CurationResponseInvalidError
from app.analysis.curation.service import CurationService
from app.models.article import Article
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


def _signal_envelope(*, raw: str = '{"relevance":"signal"}') -> CurationCall[Signal]:
    return CurationCall(
        result=Signal(title_ja="日本語タイトル", summary_ja="日本語要約"),
        raw_response=raw,
        raw_relevance="signal",
        prompt_version=GEMINI_CURATION_SPEC.version,
        model_name=GEMINI_CURATION_SPEC.model,
    )


def _noise_envelope(*, raw: str = '{"relevance":"noise"}') -> CurationCall[Noise]:
    return CurationCall(
        result=Noise(title_ja="日本語タイトル", summary_ja="日本語要約"),
        raw_response=raw,
        raw_relevance="noise",
        prompt_version=GEMINI_CURATION_SPEC.version,
        model_name=GEMINI_CURATION_SPEC.model,
    )


def _curator(
    *,
    return_envelope: CurationCall[Signal] | CurationCall[Noise] | None = None,
    side_effect=None,
) -> BaseCurator:
    mock = MagicMock(spec=BaseCurator)
    # PR4: BaseCurator の構造保証は property 契約 (model_name / prompt_version)
    type(mock).model_name = GEMINI_CURATION_SPEC.model
    type(mock).prompt_version = GEMINI_CURATION_SPEC.version
    if side_effect is not None:
        mock.curate = AsyncMock(side_effect=side_effect)
    else:
        mock.curate = AsyncMock(return_value=return_envelope or _signal_envelope())
    return mock


async def _make_article(
    db_session: AsyncSession, sample_source: NewsSource, url: str = "https://e.com/a"
) -> Article:
    article = Article(
        source_id=sample_source.id,
        source_url=url,  # type: ignore[arg-type]
        original_title="Original Title",
        original_content="content body x" * 50,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


async def _ready(article: Article) -> ReadyForCuration:
    return ReadyForCuration(
        article_id=article.id,
        original_title=article.original_title,
        original_content=article.original_content,
    )


async def _fetch_curation_events(
    db_session: AsyncSession, article_id: int
) -> list[PipelineEvent]:
    stmt = (
        select(PipelineEvent)
        .where(
            PipelineEvent.article_id == article_id,
            PipelineEvent.stage == "curation",
        )
        .order_by(PipelineEvent.id)
    )
    return list((await db_session.execute(stmt)).scalars().all())


@pytest.mark.asyncio
async def test_signal_outcome_writes_curated_signal_audit_with_category_and_code(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """signal Outcome 経路で category=success / code=curated_signal が焼かれる。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = CurationService(session_factory)

    result = await svc.execute(ready, _curator(return_envelope=_signal_envelope()))

    # signal 勝者 → Service は新規 article_extractions.id を返す
    assert isinstance(result, int)
    assert result > 0
    events = await _fetch_curation_events(db_session, article.id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "curated_signal"
    assert ev.category == "success"
    assert ev.code == "curated_signal"
    assert ev.source_id == sample_source.id
    payload = ev.payload
    assert payload["ai_model"] == GEMINI_CURATION_SPEC.model
    assert payload["prompt_version"] == GEMINI_CURATION_SPEC.version
    assert payload["source_name"] == str(sample_source.name)
    assert payload["ai_raw_response"]
    assert payload["input_content_length"] == len(article.original_content)
    # PR1-a: raw_relevance は envelope.raw_relevance から焼かれる (Stage 4 対称)
    assert payload["raw_relevance"] == "signal"


@pytest.mark.asyncio
async def test_noise_outcome_writes_curated_noise_audit_with_category_and_code(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """noise Outcome 経路で category=success / code=curated_noise が焼かれる。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = CurationService(session_factory)

    result = await svc.execute(ready, _curator(return_envelope=_noise_envelope()))

    # noise 勝者 → Service は None (Stage 4 chain しない、Task 層 short return 対象)
    assert result is None
    events = await _fetch_curation_events(db_session, article.id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "curated_noise"
    assert ev.category == "success"
    assert ev.code == "curated_noise"


@pytest.mark.asyncio
async def test_response_invalid_error_passes_through_without_service_audit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Layer 2-B 例外は Service が catch せずそのまま raise する (Task 層責務)。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = CurationService(session_factory)

    with pytest.raises(CurationResponseInvalidError):
        await svc.execute(
            ready,
            _curator(side_effect=CurationResponseInvalidError()),
        )

    # Service は audit を焼かない (失敗経路は task 層末尾の inline audit 責務、PR4)
    events = await _fetch_curation_events(db_session, article.id)
    assert len(events) == 0
