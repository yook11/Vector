"""起動ごとの接続を組み立て、Outbox relayを1回実行する。"""

from __future__ import annotations

import asyncio

from botocore.session import Session

from app.analysis.assessment.events import ArticleAssessedInScope
from app.db.engine import create_lambda_engine
from app.db.session import caller_managed_session_factory
from app.lambda_handlers.outbox_relay.settings import OutboxRelaySettings
from app.outbox.delivery.failure_handler import OutboxDeliveryFailureHandler
from app.outbox.delivery.relay import OutboxRelay
from app.outbox.publishing.assessed_in_scope import build_assessed_in_scope_message
from app.outbox.publishing.route import EventDeliveryRoute
from app.outbox.publishing.routed_publisher import RoutedEventPublisher
from app.outbox.sqs.publisher import SqsSender


async def _run_relay(settings: OutboxRelaySettings) -> dict[str, str]:
    engine = create_lambda_engine(settings)
    completed = False
    try:
        session_factory = caller_managed_session_factory(engine)
        route = EventDeliveryRoute(
            event_type=ArticleAssessedInScope.EVENT_TYPE,
            queue_url=settings.sqs_article_embedding_queue_url,
            build_message=build_assessed_in_scope_message,
        )
        sender = SqsSender.from_session(
            session=Session(),
            region=settings.aws_region,
        )
        publisher = RoutedEventPublisher(route, sender)
        failure_handler = OutboxDeliveryFailureHandler(session_factory)
        relay = OutboxRelay(
            session_factory, publisher, failure_handler, event_type=route.event_type
        )
        await relay.run_once()
        completed = True
    finally:
        try:
            await engine.dispose()
        except Exception:
            # 終了失敗で、先行する実行失敗やキャンセルを上書きしない。
            if completed:
                raise
    return {"status": "completed"}


def handler(event: object, context: object) -> dict[str, str]:
    """今回のrelay処理と接続の終了が完了した場合に応答する。"""
    settings = OutboxRelaySettings()  # type: ignore[call-arg]
    return asyncio.run(_run_relay(settings))
