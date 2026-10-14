"""予定回の対象を選び、取得依頼の未送信分だけを再送する。"""

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass

import structlog

from app.aws.sqs.errors import MessageSendError
from app.aws.sqs.message_sender import SqsMessageSender
from app.collection.article_acquisition.acquisition_retry import (
    MAX_ACQUISITION_SEND_ATTEMPTS,
    should_retry_acquisition_send,
)
from app.collection.sources.acquisition_request import (
    SourceAcquisitionRequest,
    SourceAcquisitionSchedule,
)
from app.collection.sources.dispatch import SourceDispatchService

logger = structlog.get_logger(__name__)


class AcquisitionDispatchError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _SendFailure:
    request: SourceAcquisitionRequest
    attempts: int
    error: MessageSendError


def _record_failure(failure: _SendFailure) -> None:
    try:
        logger.error(
            "source_acquisition_send_failed",
            request_id=failure.request.request_id,
            attempts=failure.attempts,
            **(failure.error.details or {}),
        )
    except Exception:  # noqa: S110 — 診断の失敗で投入失敗を上書きしない。
        pass


class SourceAcquisitionDispatcher:
    def __init__(
        self,
        *,
        dispatch_service: SourceDispatchService,
        sender_factory: Callable[[], AbstractAsyncContextManager[SqsMessageSender]],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._dispatch_service = dispatch_service
        self._sender_factory = sender_factory
        self._sleep = sleep
        self._jitter = jitter

    async def dispatch(self, schedule: SourceAcquisitionSchedule) -> None:
        targets = await self._dispatch_service.select(schedule.cadence)
        to_send = [schedule.create_request(target.id) for target in targets]
        if not to_send:
            return
        failures: list[_SendFailure] = []
        async with AsyncExitStack() as resources:
            try:
                sender = await resources.enter_async_context(self._sender_factory())
            except MessageSendError as exc:
                failures.extend(_SendFailure(request, 0, exc) for request in to_send)
            else:
                for attempt in range(1, MAX_ACQUISITION_SEND_ATTEMPTS + 1):
                    if attempt > 1:
                        await self._sleep(self._jitter() * (2 ** (attempt - 2)))
                    retry: list[SourceAcquisitionRequest] = []
                    for request in to_send:
                        try:
                            await sender.send(
                                json.dumps(request.to_message(), separators=(",", ":"))
                            )
                        except MessageSendError as exc:
                            if should_retry_acquisition_send(exc, attempt=attempt):
                                retry.append(request)
                            else:
                                failures.append(_SendFailure(request, attempt, exc))
                    to_send = retry
                    if not to_send:
                        break
        if failures:
            for failure in failures:
                _record_failure(failure)
            raise AcquisitionDispatchError
