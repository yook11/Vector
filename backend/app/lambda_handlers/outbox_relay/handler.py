"""用途別の配送を組み立て、Outbox relayを1回実行する。"""

import asyncio

from app.analysis.assessment.events import ArticleAssessedInScope
from app.analysis.curation.events import ArticleCuratedSignal
from app.lambda_handlers.outbox_relay.execution import run_relay
from app.lambda_handlers.outbox_relay.settings import (
    AssessmentOutboxRelaySettings,
    EmbeddingOutboxRelaySettings,
)
from app.outbox.publishing.assessed_in_scope import build_assessed_in_scope_message
from app.outbox.publishing.curated_signal import build_curated_signal_message
from app.outbox.publishing.route import EventDeliveryRoute


def embedding_handler(event: object, context: object) -> dict[str, str]:
    """対象内判定イベントをEmbeddingキューへ配送する。"""
    settings = EmbeddingOutboxRelaySettings()  # type: ignore[call-arg]
    route = EventDeliveryRoute(
        event_type=ArticleAssessedInScope.EVENT_TYPE,
        queue_url=settings.sqs_article_embedding_queue_url,
        build_message=build_assessed_in_scope_message,
    )
    return asyncio.run(run_relay(settings, route))


def assessment_handler(event: object, context: object) -> dict[str, str]:
    """CurationのSignalイベントをAssessmentキューへ配送する。"""
    settings = AssessmentOutboxRelaySettings()  # type: ignore[call-arg]
    route = EventDeliveryRoute(
        event_type=ArticleCuratedSignal.EVENT_TYPE,
        queue_url=settings.sqs_article_assessment_queue_url,
        build_message=build_curated_signal_message,
    )
    return asyncio.run(run_relay(settings, route))
