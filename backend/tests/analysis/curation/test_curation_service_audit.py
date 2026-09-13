"""Curationの完了結果と、結果・成功監査・Outboxの原子性を実DBで検証する。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.ai.envelope import CurationCall
from app.analysis.curation.ai.gemini_spec import GEMINI_CURATION_SPEC
from app.analysis.curation.domain import Noise, Signal
from app.analysis.curation.domain.ready import ReadyForCuration
from app.analysis.curation.errors import CurationResponseInvalidError
from app.analysis.curation.events import ArticleCuratedSignal
from app.analysis.curation.service import (
    CurationCompletion,
    CurationCompletionKind,
    CurationService,
)
from app.logfire.article_stage import curation_stage_span
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.curation_noise import CurationNoise
from app.models.news_source import NewsSource
from app.models.outbox_event import OutboxEvent
from app.models.pipeline_event import PipelineEvent
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result
from tests.logfire._span_helpers import stage_attrs
from tests.outbox import RejectOutboxInsert

_PROCESSING_OUTCOME_METRIC = "vector.curation.processing_outcome"


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
) -> AnalyzableArticleRecord:
    article = AnalyzableArticleRecord(
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


async def _ready(article: AnalyzableArticleRecord) -> ReadyForCuration:
    return ReadyForCuration(
        analyzable_article_id=article.id,
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
async def test_signal_outcome_writes_curated_signal_audit_with_outcome_code(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """signal Outcome 経路で succeeded / outcome_code=curated_signal が焼かれる。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = CurationService(session_factory)

    result = await svc.execute(ready, _curator(return_envelope=_signal_envelope()))

    # signal 勝者 → Service は新規 article_extractions.id を返す
    assert result.kind is CurationCompletionKind.SIGNAL
    assert result.curation_id > 0
    events = await _fetch_curation_events(db_session, article.id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "curated_signal"
    assert ev.retryability is None
    assert ev.source_id == sample_source.id
    payload = ev.payload
    assert payload["ai_model"] == GEMINI_CURATION_SPEC.model
    assert payload["prompt_version"] == GEMINI_CURATION_SPEC.version
    assert payload["ai_raw_response"]
    assert payload["input_content_length"] == len(article.original_content)
    # PR1-a: raw_relevance は envelope.raw_relevance から焼かれる (Stage 4 対称)
    assert payload["raw_relevance"] == "signal"


@pytest.mark.asyncio
async def test_noise_outcome_writes_curated_noise_audit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """noise Outcome 経路で succeeded / outcome_code=curated_noise が焼かれる。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = CurationService(session_factory)

    result = await svc.execute(ready, _curator(return_envelope=_noise_envelope()))

    # noise 勝者 → Service は None (Stage 4 chain しない、Task 層 short return 対象)
    assert result == CurationCompletion(CurationCompletionKind.NOISE)
    events = await _fetch_curation_events(db_session, article.id)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "curated_noise"
    assert ev.retryability is None


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


@pytest.mark.asyncio
async def test_signal_sets_stage_result_signal(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """signal 保存成功で active span に result=signal が焼かれる。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = CurationService(session_factory)

    with curation_stage_span(article_id=article.id):
        await svc.execute(ready, _curator(return_envelope=_signal_envelope()))

    assert stage_attrs(capfire)["result"] == "signal"


@pytest.mark.asyncio
async def test_noise_sets_stage_result_noise(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """noise 保存成功で active span に result=noise が焼かれる。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = CurationService(session_factory)

    with curation_stage_span(article_id=article.id):
        await svc.execute(ready, _curator(return_envelope=_noise_envelope()))

    assert stage_attrs(capfire)["result"] == "noise"


@pytest.mark.asyncio
async def test_signal_race_loss_sets_stage_result_skipped(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """signal の楽観ロック敗北 (save_signal=None) で result=skipped が焼かれる。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = CurationService(session_factory)

    with curation_stage_span(article_id=article.id):
        with patch(
            "app.analysis.curation.repository.CurationRepository.save_signal",
            new=AsyncMock(return_value=None),
        ):
            await svc.execute(ready, _curator(return_envelope=_signal_envelope()))

    assert stage_attrs(capfire)["result"] == "skipped"


# processing_outcome emit — commit 後に signal/noise、race loss は emit しない


@pytest.mark.asyncio
async def test_signal_emits_processing_outcome_signal(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """signal 保存 + commit 後に processing_outcome{result=signal} が +1 される。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = CurationService(session_factory)

    await svc.execute(ready, _curator(return_envelope=_signal_envelope()))

    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _PROCESSING_OUTCOME_METRIC, "signal") == 1
    assert sum_counter_for_result(metrics, _PROCESSING_OUTCOME_METRIC, "noise") == 0


@pytest.mark.asyncio
async def test_noise_emits_processing_outcome_noise(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """noise 保存 + commit 後に processing_outcome{result=noise} が +1 される。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = CurationService(session_factory)

    await svc.execute(ready, _curator(return_envelope=_noise_envelope()))

    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _PROCESSING_OUTCOME_METRIC, "noise") == 1
    assert sum_counter_for_result(metrics, _PROCESSING_OUTCOME_METRIC, "signal") == 0


@pytest.mark.asyncio
async def test_race_loss_does_not_emit_processing_outcome(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    capfire: CaptureLogfire,
) -> None:
    """楽観ロック敗北 (commit 未到達) では processing_outcome を emit しない。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    svc = CurationService(session_factory)

    with patch(
        "app.analysis.curation.repository.CurationRepository.save_signal",
        new=AsyncMock(return_value=None),
    ):
        await svc.execute(ready, _curator(return_envelope=_signal_envelope()))

    metrics = collected_metrics(capfire)
    for result in ("signal", "noise", "rejected", "failed", "infra_error"):
        assert sum_counter_for_result(metrics, _PROCESSING_OUTCOME_METRIC, result) == 0


@pytest.mark.asyncio
async def test_signal_persists_matching_outbox_event(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Signalの保存結果と対応する契約バージョンのイベントを同時に確定する。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)

    completion = await CurationService(session_factory).execute(
        ready, _curator(return_envelope=_signal_envelope())
    )

    async with session_factory() as reader:
        event = (
            await reader.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == ArticleCuratedSignal.EVENT_TYPE
                )
            )
        ).scalar_one()
    assert {
        "schema_version": event.schema_version,
        "payload": event.payload,
    } == {
        "schema_version": ArticleCuratedSignal.SCHEMA_VERSION,
        "payload": {
            "analyzable_article_id": article.id,
            "curation_id": completion.curation_id,
        },
    }


@pytest.mark.asyncio
async def test_noise_writes_no_outbox_event(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Noiseは後続Assessmentを起動しないためイベントを作らない。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)

    await CurationService(session_factory).execute(
        ready, _curator(return_envelope=_noise_envelope())
    )

    async with session_factory() as reader:
        event_ids = (await reader.execute(select(OutboxEvent.event_id))).scalars().all()
    assert event_ids == []


@pytest.mark.asyncio
async def test_signal_race_loss_writes_no_outbox_event(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """Signal保存の競合敗北では勝者と重複するイベントを作らない。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)

    with patch(
        "app.analysis.curation.repository.CurationRepository.save_signal",
        new=AsyncMock(return_value=None),
    ):
        await CurationService(session_factory).execute(
            ready, _curator(return_envelope=_signal_envelope())
        )

    async with session_factory() as reader:
        event_ids = (await reader.execute(select(OutboxEvent.event_id))).scalars().all()
    assert event_ids == []


@pytest.mark.asyncio
async def test_outbox_insert_failure_rolls_back_signal_and_audit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    reject_outbox_insert: RejectOutboxInsert,
) -> None:
    """Outboxの失敗でSignal結果と成功監査も原子的に取り消す。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    constraint_name = await reject_outbox_insert(ArticleCuratedSignal.EVENT_TYPE)

    with pytest.raises(IntegrityError, match=constraint_name):
        await CurationService(session_factory).execute(
            ready, _curator(return_envelope=_signal_envelope())
        )

    async with session_factory() as reader:
        curations = (
            (
                await reader.execute(
                    select(ArticleCuration.id).where(
                        ArticleCuration.analyzable_article_id == article.id
                    )
                )
            )
            .scalars()
            .all()
        )
        audit_events = (
            (
                await reader.execute(
                    select(PipelineEvent.id).where(
                        PipelineEvent.article_id == article.id,
                        PipelineEvent.stage == "curation",
                    )
                )
            )
            .scalars()
            .all()
        )
        outbox_events = (
            (await reader.execute(select(OutboxEvent.event_id))).scalars().all()
        )
    assert {
        "curations": curations,
        "audit_events": audit_events,
        "outbox_events": outbox_events,
    } == {"curations": [], "audit_events": [], "outbox_events": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("envelope_factory", [_signal_envelope, _noise_envelope])
async def test_existing_save_returns_already_curated_without_duplicate_effects(
    db_session, session_factory, sample_source, envelope_factory
):
    """保存済み行との競合は処理済み完了となり、監査とOutboxを重複させない。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    service = CurationService(session_factory)
    first = await service.execute(ready, _curator(return_envelope=envelope_factory()))
    second = await service.execute(ready, _curator(return_envelope=envelope_factory()))

    async with session_factory() as reader:
        audits = await _fetch_curation_events(reader, article.id)
        outbox = (await reader.scalars(select(OutboxEvent))).all()
        signals = (await reader.scalars(select(ArticleCuration))).all()
        noises = (await reader.scalars(select(CurationNoise))).all()
    assert second == CurationCompletion(CurationCompletionKind.ALREADY_CURATED)
    assert len(audits) == 1
    if first.kind is CurationCompletionKind.SIGNAL:
        assert (len(signals), len(noises), len(outbox)) == (1, 0, 1)
        assert signals[0].id == first.curation_id == outbox[0].payload["curation_id"]
    else:
        assert (len(signals), len(noises), len(outbox)) == (0, 1, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("envelope_factory", [_signal_envelope, _noise_envelope])
@pytest.mark.parametrize("failure_at", ["save", "audit", "commit"])
async def test_persistence_failures_propagate_and_roll_back_all_results(
    db_session, session_factory, sample_source, envelope_factory, failure_at
):
    """保存・監査・commitの失敗は完了値に変換せず、同一取引の全書き込みを戻す。"""
    article = await _make_article(db_session, sample_source)
    ready = await _ready(article)
    failure = RuntimeError("injected persistence failure")

    def fail(*args):
        raise failure

    target = None
    event_name = None
    if failure_at == "save":
        ready = ready.model_copy(update={"analyzable_article_id": article.id + 1000})
    elif failure_at == "audit":
        target, event_name = PipelineEvent, "before_insert"
    else:
        target, event_name = db_session.bind.sync_engine, "commit"
    if target is not None:
        event.listen(target, event_name, fail)
    try:
        with pytest.raises(
            IntegrityError if failure_at == "save" else RuntimeError
        ) as raised:
            await CurationService(session_factory).execute(
                ready, _curator(return_envelope=envelope_factory())
            )
        if failure_at != "save":
            assert raised.value is failure
    finally:
        if target is not None:
            event.remove(target, event_name, fail)

    async with session_factory() as reader:
        remaining = {
            model.__tablename__: (await reader.scalars(select(model))).all()
            for model in (ArticleCuration, CurationNoise, PipelineEvent, OutboxEvent)
        }
    assert remaining == {name: [] for name in remaining}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content, reason_suffix, expected_length",
    [
        pytest.param("", "input_invalid", None, id="empty-content"),
        pytest.param("秘" * 200_001, "content_too_large", 200_001, id="too-large"),
    ],
)
async def test_task_ready_rejection_keeps_article_without_ai_or_followup(
    db_session, session_factory, sample_source, content, reason_suffix, expected_length
):
    """DB事実からの入力拒否は安全な拒否監査だけを残し、記事と後続経路を保つ。"""
    from types import SimpleNamespace

    from app.queue.messages.curation import CurationTrigger
    from app.queue.tasks.curation import curate_content

    article = await _make_article(db_session, sample_source)
    article.original_content = content
    await db_session.commit()
    curator = _curator()
    ctx = SimpleNamespace(
        state=SimpleNamespace(session_factory=session_factory, curator=curator)
    )

    with patch(
        "app.queue.tasks.curation.assess_content.kiq", new_callable=AsyncMock
    ) as enqueue:
        await curate_content(CurationTrigger(analyzable_article_id=article.id), ctx)
    curator.curate.assert_not_awaited()
    enqueue.assert_not_awaited()
    async with session_factory() as reader:
        assert await reader.get(AnalyzableArticleRecord, article.id) is not None
        (audit,) = await _fetch_curation_events(reader, article.id)
        assert audit.event_type == "rejected"
        assert audit.outcome_code == "curation_ready_build_blocked_" + reason_suffix
        assert audit.source_id == sample_source.id
        assert audit.payload["input_content_length"] == expected_length
        assert audit.payload["max_content_length"] == (
            200_000 if expected_length else None
        )
        assert all(
            audit.payload.get(key) is None
            for key in ("input_content_head", "error_message", "error_chain")
        )
        assert "秘" not in repr(audit.payload)
        for model in (ArticleCuration, CurationNoise, OutboxEvent):
            assert (await reader.scalars(select(model))).all() == []


@pytest.mark.asyncio
async def test_task_provider_failure_preserves_service_cause_through_real_audit(
    db_session, session_factory, sample_source
):
    """実Serviceのプロバイダー障害を旧Taskiq分類へ接続し、監査まで原因を保持する。"""
    from types import SimpleNamespace

    from app.ai_providers.errors import AIProviderNetworkError
    from app.analysis.curation.errors import CurationError
    from app.analysis.curation.task_errors import CurationRecoverableError
    from app.queue.messages.curation import CurationTrigger
    from app.queue.tasks.curation import curate_content

    article = await _make_article(db_session, sample_source)
    provider = AIProviderNetworkError()
    ctx = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=session_factory, curator=_curator(side_effect=provider)
        ),
        message=SimpleNamespace(labels={"_retries": 0, "max_retries": 2}),
    )
    with pytest.raises(CurationRecoverableError) as raised:
        await curate_content(CurationTrigger(analyzable_article_id=article.id), ctx)
    business = raised.value.__cause__
    assert isinstance(business, CurationError)
    assert business.__cause__ is provider
    assert raised.value.provider_error is business.provider_error is provider
    async with session_factory() as reader:
        (audit,) = await _fetch_curation_events(reader, article.id)
        assert audit.outcome_code == provider.CODE
        assert audit.event_type == "failed"
        chain = repr(audit.payload["error_chain"])
        assert all(
            name in chain
            for name in (
                "CurationRecoverableError",
                "CurationError",
                "AIProviderNetworkError",
            )
        )
