"""工程別 backfill が pipeline_stage span を自工程の stage / op で開く配線。"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire

from app.audit.domain.event import Stage
from app.backfill import service
from tests.logfire._span_helpers import pipeline_stage_attrs

NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)

CASES = [
    (
        service.backfill_curations,
        "app.backfill.service.delete_aged_out_curations",
        Stage.BACKFILL_CURATE,
        "backfill_curations",
    ),
    (
        service.backfill_assessments,
        "app.backfill.service.exclude_aged_out_assessments",
        Stage.BACKFILL_ASSESS,
        "backfill_assessments",
    ),
    (
        service.backfill_embeddings,
        "app.backfill.service.exclude_aged_out_embeddings",
        Stage.BACKFILL_EMBED,
        "backfill_embeddings",
    ),
]


def _session_factory() -> MagicMock:
    @asynccontextmanager
    async def _session():
        yield MagicMock()

    return MagicMock(side_effect=_session)


@pytest.mark.asyncio
@pytest.mark.parametrize(("entry", "ageout_patch", "stage", "op"), CASES)
async def test_enabled_run_opens_one_span_with_own_stage_and_op(
    capfire: CaptureLogfire, entry, ageout_patch, stage, op
) -> None:
    """有効な実行は、自工程の stage と op を持つ span をちょうど 1 件開く。"""
    # 対象 0 件の実行でも span は開くため、照会結果は空で足りる。
    backlog = MagicMock()
    backlog.configure_mock(
        **{
            name: AsyncMock(return_value=value)
            for name, value in {
                "count_articles_pending_curation": 0,
                "curation_events_pending": [],
                "count_curations_pending_assessment": 0,
                "assessment_events_pending": [],
                "count_analyzed_articles_pending_embedding": 0,
                "embedding_events_pending": [],
            }.items()
        }
    )
    with (
        patch(ageout_patch, AsyncMock(return_value=0)),
        patch("app.backfill.service.PipelineBacklog", return_value=backlog),
    ):
        await entry(_session_factory(), MagicMock(), enabled=True, now=NOW)

    attrs = pipeline_stage_attrs(capfire)
    assert attrs["stage"] == stage.value
    assert attrs["op"] == op
