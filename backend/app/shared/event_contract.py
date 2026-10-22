"""工程間で送受信するイベントの契約違反を、入力値を含めずに表す。"""

from dataclasses import dataclass
from enum import StrEnum


class EventInvalidReason(StrEnum):
    """入力値を含めずに共有できる、イベント契約違反の理由。"""

    INVALID_ENVELOPE = "invalid_envelope"
    UNSUPPORTED_EVENT_TYPE = "unsupported_event_type"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    INVALID_PAYLOAD = "invalid_payload"


class EventInvalidCode(StrEnum):
    """入力値や検証ライブラリの自由文に依存しない違反コード。"""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"
    UNKNOWN_FIELD = "unknown_field"
    UNSUPPORTED_EVENT_TYPE = "unsupported_event_type"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"


@dataclass(frozen=True, slots=True)
class EventInvalidIssue:
    """入力の内容を保持しない、一項目の契約違反で、項目は各イベントの契約が定義する。"""

    field: StrEnum
    code: EventInvalidCode


@dataclass(frozen=True, slots=True)
class EventInvalid:
    """優先する失敗理由と、重複のない安全な検証詳細。"""

    reason: EventInvalidReason
    issues: tuple[EventInvalidIssue, ...]


class EventInvalidError(Exception):
    """イベントの構築失敗を、安全な理由と検証詳細で伝え、種別ごとの契約がサブクラスを送出する。"""

    def __init__(self, invalid: EventInvalid) -> None:
        super().__init__()
        self.invalid = invalid
