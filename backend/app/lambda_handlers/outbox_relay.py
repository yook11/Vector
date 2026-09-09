"""起動ごとの接続を組み立て、Outbox relayを1回実行する。"""

from __future__ import annotations

import asyncio

from botocore.session import Session

from app.db.engine import create_lambda_engine
from app.db.session import caller_managed_session_factory
from app.lambda_handlers.settings import OutboxRelaySettings
from app.outbox.delivery.failure_handler import PublishFailureHandler
from app.outbox.delivery.relay import OutboxRelay
from app.outbox.sqs.publisher import SqsEventPublisher


async def _run_relay(settings: OutboxRelaySettings) -> dict[str, str]:
    engine = create_lambda_engine(settings)
    completed = False
    try:
        session_factory = caller_managed_session_factory(engine)
        publisher = SqsEventPublisher.from_session(
            session=Session(),
            region=settings.aws_region,
            embedding_queue_url=settings.sqs_article_embedding_queue_url,
        )
        failure_handler = PublishFailureHandler(session_factory)
        relay = OutboxRelay(session_factory, publisher, failure_handler)
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
