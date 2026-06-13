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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.service import EmbeddingService
from app.logfire.article_stage import embedding_stage_span
from tests.logfire._span_helpers import stage_attrs


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
        article_id=7,
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
