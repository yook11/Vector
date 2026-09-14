"""接続を借りて本体を一度実行し、続行不能な失敗を呼び出し元へ伝える。"""

from collections.abc import Awaitable
from datetime import datetime
from typing import Protocol

from app.db.engine import BackfillStage
from app.db.session import SessionFactory
from app.lambda_handlers.backfill.failure_recorder import record_failure
from app.lambda_handlers.backfill.resources import open_backfill_resources
from app.lambda_handlers.backfill.settings import BackfillConnectionSettings
from app.outbox.publishing.publisher import EventPublisher
from app.outbox.publishing.route import EventDeliveryRoute


class BackfillOperation(Protocol):
    def __call__(
        self,
        session_factory: SessionFactory,
        publisher: EventPublisher,
        *,
        enabled: bool,
        now: datetime,
    ) -> Awaitable[None]: ...


async def run_backfill(
    settings: BackfillConnectionSettings,
    *,
    stage: BackfillStage,
    route: EventDeliveryRoute,
    operation: BackfillOperation,
    now: datetime,
) -> None:
    async with open_backfill_resources(settings, stage=stage, route=route) as resources:
        try:
            await operation(
                session_factory=resources.session_factory,
                publisher=resources.publisher,
                enabled=True,
                now=now,
            )
        except BaseException as exc:
            record_failure(stage, "execution", exc)
            raise
