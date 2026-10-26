"""配送定義を受け取り、起動ごとの資源でrelayを実行する。"""

from __future__ import annotations

from collections.abc import Callable

from botocore.session import Session

from app.aws.sqs.message_sender import create_sqs_client, open_sqs_message_sender
from app.db.engine import create_lambda_engine
from app.db.session import caller_managed_session_factory
from app.lambda_handlers.outbox_relay.settings import OutboxRelayConnectionSettings
from app.outbox.event.envelope import EventEnvelope
from app.outbox.event.message import EventMessage
from app.outbox.relay.failure_handler import OutboxDeliveryFailureHandler
from app.outbox.relay.relay import OutboxRelay
from app.outbox.relay.route import EventDeliveryRoute


async def run_relay(
    settings: OutboxRelayConnectionSettings,
    route: EventDeliveryRoute,
    build_message: Callable[[EventEnvelope], EventMessage],
) -> dict[str, str]:
    engine = create_lambda_engine(settings)
    completed = False
    try:
        session_factory = caller_managed_session_factory(engine)
        sqs_session = Session()
        failure_handler = OutboxDeliveryFailureHandler(session_factory)
        relay = OutboxRelay(
            session_factory,
            lambda: open_sqs_message_sender(
                client_factory=lambda: create_sqs_client(
                    session=sqs_session, region=settings.aws_region, max_attempts=1
                ),
                queue_url=route.queue_url,
            ),
            failure_handler,
            route=route,
            build_message=build_message,
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
