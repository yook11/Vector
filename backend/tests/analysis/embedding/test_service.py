"""``EmbeddingService`` の ``article_stage`` span result 配線 (正本)。

実 Service が ``EmbeddingRepository.save`` の戻り (saved 真偽) に応じて
``set_embedding_stage_result`` を ``succeeded`` / ``skipped`` のどちらで呼ぶかを
capfire の span result で固定する。result 語彙の分岐ロジックを検証対象とするため、
save / audit append は patch で制御し (どの分岐に入るかだけを与える)、embedder の
AI 呼び出しも mock する。session_factory は実 DB fixture を使う (integration 分類)。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.service import EmbeddingService
from app.logfire.article_stage import embedding_stage_span
from tests.logfire._metric_helpers import collected_metrics, sum_counter_for_result
from tests.logfire._span_helpers import stage_attrs

_METRIC = "vector.embedding.processing_outcome"
_ALL_RESULTS = ("succeeded", "failed", "infra_error")


def _make_embedder() -> MagicMock:
    fake = MagicMock(spec=BaseEmbedder)
    fake.model_name = "gemini-embedding-001"
    fake.dimension = 768
    # save が patch されるため戻り vector の中身は問わない。
    fake.embed_document = AsyncMock(return_value=[0.0] * 768)
    return fake


def _ready() -> ReadyForEmbedding:
    return ReadyForEmbedding(
        analyzed_article_id=1,
        text_for_embedding="分析タイトル\n分析要約",
        analyzable_article_id=7,
    )


@pytest.mark.asyncio
async def test_save_success_sets_stage_result_succeeded(
    session_factory: async_sessionmaker[AsyncSession],
    capfire: CaptureLogfire,
) -> None:
    """save 成功 (saved=True) で active span に result=succeeded が焼かれる。"""
    svc = EmbeddingService(session_factory)
    with embedding_stage_span(analyzed_article_id=1):
        with (
            patch(
                "app.analysis.embedding.repository.EmbeddingRepository.save",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.analysis.embedding.service.EmbeddingAuditRepository"
            ) as mock_audit,
        ):
            mock_audit.return_value.append_success = AsyncMock()
            await svc.execute(_ready(), _make_embedder())

    assert stage_attrs(capfire)["result"] == "succeeded"


@pytest.mark.asyncio
async def test_save_success_emits_processing_outcome_succeeded(
    session_factory: async_sessionmaker[AsyncSession],
    capfire: CaptureLogfire,
) -> None:
    """save 成功 (saved=True) で processing_outcome{result=succeeded} が +1 される。"""
    svc = EmbeddingService(session_factory)
    with embedding_stage_span(analyzed_article_id=1):
        with (
            patch(
                "app.analysis.embedding.repository.EmbeddingRepository.save",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.analysis.embedding.service.EmbeddingAuditRepository"
            ) as mock_audit,
        ):
            mock_audit.return_value.append_success = AsyncMock()
            await svc.execute(_ready(), _make_embedder())

    metrics = collected_metrics(capfire)
    assert sum_counter_for_result(metrics, _METRIC, "succeeded") == 1
    for other in ("failed", "infra_error"):
        assert sum_counter_for_result(metrics, _METRIC, other) == 0


@pytest.mark.asyncio
async def test_race_loss_sets_stage_result_skipped(
    session_factory: async_sessionmaker[AsyncSession],
    capfire: CaptureLogfire,
) -> None:
    """楽観ロック敗北 (save=False) で active span に result=skipped が焼かれる。"""
    svc = EmbeddingService(session_factory)
    with embedding_stage_span(analyzed_article_id=1):
        with patch(
            "app.analysis.embedding.repository.EmbeddingRepository.save",
            new=AsyncMock(return_value=False),
        ):
            await svc.execute(_ready(), _make_embedder())

    assert stage_attrs(capfire)["result"] == "skipped"


@pytest.mark.asyncio
async def test_race_loss_does_not_emit_processing_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    capfire: CaptureLogfire,
) -> None:
    """楽観ロック敗北 (save=False) は処理成功でも失敗でもなく counter を汚さない。"""
    svc = EmbeddingService(session_factory)
    with embedding_stage_span(analyzed_article_id=1):
        with patch(
            "app.analysis.embedding.repository.EmbeddingRepository.save",
            new=AsyncMock(return_value=False),
        ):
            await svc.execute(_ready(), _make_embedder())

    metrics = collected_metrics(capfire)
    for result in _ALL_RESULTS:
        assert sum_counter_for_result(metrics, _METRIC, result) == 0


@pytest.mark.asyncio
async def test_commit_failure_does_not_emit_succeeded(
    session_factory: async_sessionmaker[AsyncSession],
    capfire: CaptureLogfire,
) -> None:
    """save 後に commit が落ちたら succeeded を emit しない (例外は伝播する)。

    succeeded emit が「業務 UPDATE + audit を commit できた」境界を担う不変条件の回帰
    ガード。emit は commit の後ろにあり、commit 例外は emit 前に execute() を貫通する。
    """
    svc = EmbeddingService(session_factory)
    with embedding_stage_span(analyzed_article_id=1):
        with (
            patch(
                "app.analysis.embedding.repository.EmbeddingRepository.save",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.analysis.embedding.service.EmbeddingAuditRepository"
            ) as mock_audit,
            patch.object(
                AsyncSession,
                "commit",
                new=AsyncMock(side_effect=SQLAlchemyError("commit boom")),
            ),
        ):
            mock_audit.return_value.append_success = AsyncMock()
            with pytest.raises(SQLAlchemyError):
                await svc.execute(_ready(), _make_embedder())

    metrics = collected_metrics(capfire)
    for result in _ALL_RESULTS:
        assert sum_counter_for_result(metrics, _METRIC, result) == 0
