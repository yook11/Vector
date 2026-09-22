"""基準時刻と工程を固定して、定期実行のbackfillを開始する。"""

import asyncio

from app.analysis.assessment.events import ArticleAssessedInScope
from app.analysis.curation.events import ArticleCuratedSignal
from app.backfill.service import (
    backfill_assessments,
    backfill_completions,
    backfill_curations,
    backfill_embeddings,
)
from app.collection.article_acquisition.events import IncompleteArticleRecorded
from app.collection.events import AnalyzableArticleCreated
from app.db.engine import BackfillStage
from app.lambda_handlers.backfill.execution import run_backfill
from app.lambda_handlers.backfill.failure_recorder import record_failure
from app.lambda_handlers.backfill.settings import (
    AssessmentBackfillSettings,
    BackfillConnectionSettings,
    CompletionBackfillSettings,
    CurationBackfillSettings,
    EmbeddingBackfillSettings,
)
from app.lambda_handlers.logging import setup_lambda_logging
from app.outbox.publishing.analyzable_created import build_analyzable_created_message
from app.outbox.publishing.assessed_in_scope import build_assessed_in_scope_message
from app.outbox.publishing.curated_signal import build_curated_signal_message
from app.outbox.publishing.incomplete_recorded import (
    build_incomplete_recorded_message,
)
from app.outbox.publishing.route import EventDeliveryRoute
from app.shared.time import utc_now


def _load_settings[SettingsT: BackfillConnectionSettings](
    settings_type: type[SettingsT], stage: BackfillStage
) -> SettingsT:
    setup_lambda_logging()
    try:
        return settings_type()  # type: ignore[call-arg]
    except Exception as exc:
        record_failure(stage, "settings", exc)
        raise


def curation_handler(event: object, context: object) -> None:
    """curationの起動時刻を固定し、有効なら一度だけ実行する。"""
    now = utc_now()
    settings = _load_settings(CurationBackfillSettings, "curation")
    if not settings.backfill_curations_enabled:
        return
    route = EventDeliveryRoute(
        event_type=AnalyzableArticleCreated.EVENT_TYPE,
        queue_url=settings.sqs_article_curation_queue_url,
        build_message=build_analyzable_created_message,
    )
    asyncio.run(
        run_backfill(
            settings,
            stage="curation",
            route=route,
            operation=backfill_curations,
            now=now,
        )
    )


def assessment_handler(event: object, context: object) -> None:
    """assessmentの起動時刻を固定し、有効なら一度だけ実行する。"""
    now = utc_now()
    settings = _load_settings(AssessmentBackfillSettings, "assessment")
    if not settings.backfill_assessments_enabled:
        return
    route = EventDeliveryRoute(
        event_type=ArticleCuratedSignal.EVENT_TYPE,
        queue_url=settings.sqs_article_assessment_queue_url,
        build_message=build_curated_signal_message,
    )
    asyncio.run(
        run_backfill(
            settings,
            stage="assessment",
            route=route,
            operation=backfill_assessments,
            now=now,
        )
    )


def embedding_handler(event: object, context: object) -> None:
    """embeddingの起動時刻を固定し、有効なら一度だけ実行する。"""
    now = utc_now()
    settings = _load_settings(EmbeddingBackfillSettings, "embedding")
    if not settings.backfill_embeddings_enabled:
        return
    route = EventDeliveryRoute(
        event_type=ArticleAssessedInScope.EVENT_TYPE,
        queue_url=settings.sqs_article_embedding_queue_url,
        build_message=build_assessed_in_scope_message,
    )
    asyncio.run(
        run_backfill(
            settings,
            stage="embedding",
            route=route,
            operation=backfill_embeddings,
            now=now,
        )
    )


def completion_handler(event: object, context: object) -> None:
    """補完救済の起動時刻を固定し、有効なら一度だけ実行する。"""
    now = utc_now()
    settings = _load_settings(CompletionBackfillSettings, "completion")
    if not settings.backfill_completions_enabled:
        return
    route = EventDeliveryRoute(
        event_type=IncompleteArticleRecorded.EVENT_TYPE,
        queue_url=settings.sqs_article_completion_queue_url,
        build_message=build_incomplete_recorded_message,
    )
    asyncio.run(
        run_backfill(
            settings,
            stage="completion",
            route=route,
            operation=backfill_completions,
            now=now,
        )
    )
