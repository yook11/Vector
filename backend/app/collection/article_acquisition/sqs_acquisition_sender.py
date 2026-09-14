"""SQSの送信失敗を取得依頼の送信失敗へ変換する。"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from app.aws.sqs.errors import SqsSendError, SqsSendFailure, SqsSendFailureKind
from app.aws.sqs.message_sender import (
    SqsClient,
    SqsMessageSender,
    open_sqs_message_sender,
)
from app.collection.article_acquisition.acquisition_send_error import (
    AcquisitionSendFailure,
    RetryDisposition,
    SourceAcquisitionSendError,
)
from app.http.failure import HttpTransportFailureReason


def _is_retryable_transport(failure: SqsSendFailure) -> bool:
    if failure.kind is not SqsSendFailureKind.TRANSPORT or failure.transport is None:
        return False
    if failure.transport.reason is HttpTransportFailureReason.PROXY:
        status = failure.transport.proxy_status
        return status is None or status == 429 or 500 <= status <= 599
    return failure.transport.reason in {
        HttpTransportFailureReason.TIMEOUT,
        HttpTransportFailureReason.DNS_RESOLUTION,
        HttpTransportFailureReason.NETWORK_IO,
        HttpTransportFailureReason.PROTOCOL_VIOLATION,
        HttpTransportFailureReason.UNKNOWN,
    }


def _disposition(failure: SqsSendFailure) -> RetryDisposition:
    if failure.kind in (
        SqsSendFailureKind.THROTTLED,
        SqsSendFailureKind.SERVICE_UNAVAILABLE,
    ) or _is_retryable_transport(failure):
        return RetryDisposition.RETRYABLE
    if failure.kind in (
        SqsSendFailureKind.CONFIGURATION,
        SqsSendFailureKind.REQUEST_REJECTED,
        SqsSendFailureKind.TRANSPORT,
    ):
        return RetryDisposition.NON_RETRYABLE
    return RetryDisposition.INVESTIGATION_REQUIRED


def to_acquisition_send_error(error: SqsSendError) -> SourceAcquisitionSendError:
    failure = error.failure
    return SourceAcquisitionSendError(
        AcquisitionSendFailure(
            disposition=_disposition(failure),
            kind=failure.kind.value,
            service_code=failure.service_code,
            exception_type=failure.exception_type,
            transport_reason=failure.transport.reason.value
            if failure.transport
            else None,
        )
    )


class SqsSourceAcquisitionSender:
    def __init__(self, sender: SqsMessageSender) -> None:
        self._sender = sender

    async def send(self, body: str) -> None:
        try:
            await self._sender.send(body)
        except SqsSendError as exc:
            raise to_acquisition_send_error(exc) from None


@asynccontextmanager
async def open_acquisition_sender(
    *, client_factory: Callable[[], SqsClient], queue_url: str
) -> AsyncIterator[SqsSourceAcquisitionSender]:
    try:
        async with open_sqs_message_sender(
            client_factory=client_factory, queue_url=queue_url
        ) as sender:
            yield SqsSourceAcquisitionSender(sender)
    except SqsSendError as exc:
        raise to_acquisition_send_error(exc) from None
