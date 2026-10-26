"""予定回の対象を選び、取得依頼の未送信分だけを再送する。"""

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass

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
from app.log_policy.policies.pipeline_control import PIPELINE_CONTROL_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger
from app.outbox.relay.retry_policy import is_resolvable_by_retry
from app.outbox.relay.sender import MessageSendError
from app.shared.errors import ApplicationError


class AcquisitionDispatchError(ApplicationError):
    """送れなかった件数を持ち、再試行してよい失敗だけなら調査不要とする。"""

    def __init__(
        self, *, target_count: int, failed_count: int, requires_investigation: bool
    ) -> None:
        super().__init__(
            "取得依頼を送信できなかったものがあります",
            details={"target_count": target_count, "failed_count": failed_count},
        )
        self.requires_investigation = requires_investigation


@dataclass(frozen=True, slots=True)
class _SendFailure:
    request: SourceAcquisitionRequest
    attempts: int
    error: MessageSendError


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

    async def dispatch(self, schedule: SourceAcquisitionSchedule) -> int:
        targets = await self._dispatch_service.select(schedule.cadence)
        to_send = [schedule.create_request(target.id) for target in targets]
        target_count = len(to_send)
        if not to_send:
            return 0
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
            logger = create_policy_json_logger(__name__, PIPELINE_CONTROL_LOG_RULES)
            for failure in failures:
                # 再試行してよい失敗を回数で諦めただけなら想定内として warning にする。
                (
                    logger.warning
                    if is_resolvable_by_retry(failure.error)
                    else logger.error
                )(
                    "source_acquisition_send_failed",
                    acquisition_request_id=failure.request.request_id,
                    source_id=failure.request.source_id,
                    attempts=failure.attempts,
                    exc_info=failure.error,
                )
            raise AcquisitionDispatchError(
                target_count=target_count,
                failed_count=len(failures),
                requires_investigation=any(
                    not is_resolvable_by_retry(failure.error) for failure in failures
                ),
            )
        return target_count
