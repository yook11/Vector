"""取得依頼の送信失敗と、自動再送の扱いを表す。"""

from dataclasses import dataclass
from enum import StrEnum

from app.logfire.exceptions import VectorDomainError


class RetryDisposition(StrEnum):
    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"
    INVESTIGATION_REQUIRED = "investigation_required"


@dataclass(frozen=True, slots=True)
class AcquisitionSendFailure:
    disposition: RetryDisposition
    kind: str
    service_code: str | None = None
    exception_type: str | None = None
    transport_reason: str | None = None


class SourceAcquisitionSendError(VectorDomainError):
    def __init__(self, failure: AcquisitionSendFailure) -> None:
        super().__init__()
        self.failure = failure
