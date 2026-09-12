"""配送定義を受け取り、起動ごとの資源でrelayを実行する。"""

from __future__ import annotations

from botocore.session import Session

from app.db.engine import create_lambda_engine
from app.db.session import caller_managed_session_factory
from app.lambda_handlers.outbox_relay.settings import OutboxRelayConnectionSettings
from app.outbox.delivery.failure_handler import OutboxDeliveryFailureHandler
from app.outbox.delivery.relay import OutboxRelay
from app.outbox.publishing.route import EventDeliveryRoute
from app.outbox.publishing.routed_publisher import RoutedEventPublisher
from app.outbox.sqs.publisher import SqsSender


async def run_relay(
    settings: OutboxRelayConnectionSettings, route: EventDeliveryRoute
) -> dict[str, str]:
    engine = create_lambda_engine(settings)
    completed = False
    try:
        session_factory = caller_managed_session_factory(engine)
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
