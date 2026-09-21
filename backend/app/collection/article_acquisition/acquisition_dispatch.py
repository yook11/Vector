"""予定回の対象を選び、取得依頼の未送信分だけを再送する。"""

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass
from typing import Protocol

import structlog

from app.collection.article_acquisition.acquisition_retry import (
    MAX_ACQUISITION_SEND_ATTEMPTS,
    should_retry_acquisition_send,
)
from app.collection.article_acquisition.acquisition_send_error import (
    AcquisitionSendFailure,
    SourceAcquisitionSendError,
)
from app.collection.sources.acquisition_request import (
    SourceAcquisitionRequest,
    SourceAcquisitionSchedule,
)
from app.collection.sources.dispatch import SourceDispatchService

logger = structlog.get_logger(__name__)


class SourceAcquisitionSender(Protocol):
    """確定した取得依頼本文を送り、受付失敗を呼び出し側へ伝える契約。"""

    async def send(self, body: str) -> None: ...


@dataclass(frozen=True, slots=True)
class UnsentAcquisitionRequest:
    request_id: str
    attempts: int
    failure: AcquisitionSendFailure


class AcquisitionDispatchError(Exception):
    def __init__(self, unsent: tuple[UnsentAcquisitionRequest, ...]) -> None:
        super().__init__()
        self.unsent = unsent


@dataclass(frozen=True, slots=True)
class _PendingRequest:
    request: SourceAcquisitionRequest
    body: str


def _record_unsent(unsent: UnsentAcquisitionRequest) -> None:
    failure = unsent.failure
    try:
        logger.error(
            "source_acquisition_send_failed",
            request_id=unsent.request_id,
            attempts=unsent.attempts,
            failure_kind=failure.kind,
            service_code=failure.service_code,
            exception_type=failure.exception_type,
            transport_reason=failure.transport_reason,
        )
    except Exception:  # noqa: S110 — 診断の失敗で投入失敗を上書きしない。
        pass


class SourceAcquisitionDispatcher:
    def __init__(
        self,
        *,
        dispatch_service: SourceDispatchService,
        sender_factory: Callable[
            [], AbstractAsyncContextManager[SourceAcquisitionSender]
        ],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._dispatch_service = dispatch_service
        self._sender_factory = sender_factory
        self._sleep = sleep
        self._jitter = jitter

    async def dispatch(self, schedule: SourceAcquisitionSchedule) -> None:
        selection = await self._dispatch_service.select(schedule.cadence)
        requests = tuple(
            schedule.create_request(source.id) for source in selection.targets
        )
        pending = [
            _PendingRequest(
                request, json.dumps(request.to_message(), separators=(",", ":"))
            )
            for request in requests
        ]
        if not pending:
            return
        unsent: list[UnsentAcquisitionRequest] = []
        async with AsyncExitStack() as resources:
            try:
                sender = await resources.enter_async_context(self._sender_factory())
            except SourceAcquisitionSendError as exc:
                failure = exc.failure
                unsent.extend(
                    UnsentAcquisitionRequest(item.request.request_id, 0, failure)
                    for item in pending
                )
            else:
                for attempt in range(1, MAX_ACQUISITION_SEND_ATTEMPTS + 1):
                    if attempt > 1:
                        await self._sleep(self._jitter() * (2 ** (attempt - 2)))
                    retry: list[_PendingRequest] = []
                    for item in pending:
                        try:
                            await sender.send(item.body)
                        except SourceAcquisitionSendError as exc:
                            failure = exc.failure
                            if should_retry_acquisition_send(failure, attempt=attempt):
                                retry.append(item)
                            else:
                                unsent.append(
                                    UnsentAcquisitionRequest(
                                        item.request.request_id, attempt, failure
                                    )
                                )
                    pending = retry
                    if not pending:
                        break
        if unsent:
            for item in unsent:
                _record_unsent(item)
            raise AcquisitionDispatchError(tuple(unsent))
