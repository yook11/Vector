"""イベント送信が失敗した原因を表し、再試行や配信停止の判断は持たない。"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

from app.logfire.exceptions import VectorDomainError

if TYPE_CHECKING:
    from app.http.failure import HttpTransportFailure


class PublishConfigurationReason(StrEnum):
    """送信に必要な資格情報または設定が不足している理由。"""

    MISSING_CREDENTIALS = "missing_credentials"
    INCOMPLETE_CREDENTIALS = "incomplete_credentials"
    MISSING_REGION = "missing_region"
    CREDENTIALS_RETRIEVAL_FAILED = "credentials_retrieval_failed"


class PublishEventInvalidReason(StrEnum):
    """イベントを送信内容として扱えない理由。"""

    UNSUPPORTED_EVENT_TYPE = "unsupported_event_type"
    INVALID_OCCURRED_AT = "invalid_occurred_at"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    INVALID_ENVELOPE = "invalid_envelope"
    INVALID_PAYLOAD = "invalid_payload"
    SERIALIZATION_FAILED = "serialization_failed"
    MESSAGE_TOO_LARGE = "message_too_large"


class PublishIntegrityReason(StrEnum):
    """送信本文と送信先の受信結果の整合性を確認できない理由。"""

    BODY_CHECKSUM_MISMATCH = "body_checksum_mismatch"


class PublishResponseInvalidReason(StrEnum):
    """応答の形式または送信対象との対応を確認できない理由。"""

    INVALID_TYPE = "invalid_type"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    EMPTY_REQUIRED_FIELD = "empty_required_field"
    INVALID_CHECKSUM_FORMAT = "invalid_checksum_format"
    UNKNOWN_ENTRY_ID = "unknown_entry_id"
    DUPLICATE_ENTRY_ID = "duplicate_entry_id"
    MISSING_ENTRY_ID = "missing_entry_id"


class PublishServiceReason(StrEnum):
    """送信先の応答から分かる、実装に依存しない失敗理由。"""

    THROTTLED = "throttled"
    AUTHENTICATION_FAILED = "authentication_failed"
    ACCESS_DENIED = "access_denied"
    DESTINATION_NOT_FOUND = "destination_not_found"
    REQUEST_REJECTED = "request_rejected"
    SECURITY_REJECTED = "security_rejected"
    REQUEST_EXPIRED = "request_expired"
    ENCRYPTION_ERROR = "encryption_error"
    SERVICE_UNAVAILABLE = "service_unavailable"
    UNCLASSIFIED = "unclassified"


class PublishPhase(StrEnum):
    """送信操作の失敗が発生した段階。"""

    INITIALIZE = "initialize"
    PREPARE_EVENT = "prepare_event"
    RESOLVE_CREDENTIALS = "resolve_credentials"
    SEND = "send"
    CLASSIFY_FAILURE = "classify_failure"


class PublishError(VectorDomainError):
    """想定外の失敗を含む、イベント送信失敗の共通祖先。"""

    CODE: ClassVar[str] = "publish_error"
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE",)


class PublishTransportError(PublishError):
    """通信失敗の分類結果を保持し、イベント送信の失敗として伝える。"""

    CODE: ClassVar[str] = "publish_transport_error"
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE", "failure")

    def __init__(self, *, failure: HttpTransportFailure) -> None:
        super().__init__()
        self.failure = failure


class PublishServiceError(PublishError):
    """送信先のエラー応答を共通理由と調査用の情報で表す。"""

    CODE: ClassVar[str] = "publish_service_error"
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE", "reason", "status_code")

    def __init__(
        self,
        *,
        reason: PublishServiceReason,
        service_error_code: str,
        status_code: int | None,
        request_id: str | None = None,
    ) -> None:
        super().__init__()
        # 外部由来のコードは判断用に保持するが、自由文字列のため例外文面には出さない。
        self.reason = reason
        self.request_id = request_id
        self.service_error_code = service_error_code
        self.status_code = status_code


class PublishConfigurationError(PublishError):
    """送信に必要な資格情報または設定が不足している。"""

    CODE: ClassVar[str] = "publish_configuration_error"
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE", "reason")

    def __init__(self, *, reason: PublishConfigurationReason) -> None:
        super().__init__()
        self.reason = reason


class PublishEventInvalidError(PublishError):
    """イベントを送信内容として扱えない。"""

    CODE: ClassVar[str] = "publish_event_invalid"
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE", "reason")

    def __init__(self, *, reason: PublishEventInvalidReason) -> None:
        super().__init__()
        self.reason = reason


class PublishIntegrityError(PublishError):
    """本文の整合性確認失敗で、送信先が未受付とは断定しない。"""

    CODE: ClassVar[str] = "publish_integrity_error"
    MESSAGE: ClassVar[str] = (
        "送信本文と、送信先が受け取った本文のチェックサムが一致しません。"
    )
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE", "reason", "MESSAGE")

    def __init__(
        self, *, reason: PublishIntegrityReason, request_id: str | None = None
    ) -> None:
        super().__init__()
        self.reason = reason
        self.request_id = request_id


class PublishResponseInvalidError(PublishError):
    """応答を検証できない失敗で、送信先が未受付とは断定しない。"""

    CODE: ClassVar[str] = "publish_response_invalid"
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE", "reason")

    def __init__(self, *, reason: PublishResponseInvalidReason) -> None:
        super().__init__()
        self.reason = reason


class PublishUnexpectedError(PublishError):
    """分類できない失敗の型と発生段階を保持する。"""

    CODE: ClassVar[str] = "publish_unexpected_error"
    reason: ClassVar[str] = "unexpected_exception"
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = (
        "CODE",
        "reason",
        "original_exception_type",
        "phase",
        "classification_exception_type",
    )

    def __init__(
        self,
        *,
        original_exception: Exception,
        phase: PublishPhase,
        classification_exception: Exception | None = None,
    ) -> None:
        super().__init__()
        self.original_exception_type = (
            f"{type(original_exception).__module__}."
            f"{type(original_exception).__qualname__}"
        )
        self.phase = phase
        self.classification_exception_type = (
            f"{type(classification_exception).__module__}."
            f"{type(classification_exception).__qualname__}"
            if classification_exception is not None
            else None
        )


class PublishCleanupError(VectorDomainError):
    """送信結果とは独立した、クライアント終了処理の失敗を表す。"""

    CODE: ClassVar[str] = "publish_cleanup_error"
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE", "original_exception_type")

    def __init__(self, *, original_exception: Exception) -> None:
        super().__init__()
        self.original_exception_type = (
            f"{type(original_exception).__module__}."
            f"{type(original_exception).__qualname__}"
        )
