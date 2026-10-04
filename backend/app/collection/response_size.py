"""外部取得の応答本文が上限を超えたことを、何で確認したかを表す。"""

from enum import StrEnum


class ResponseSizeBasis(StrEnum):
    """上限超過を確認したサイズの根拠。"""

    DECLARED_CONTENT_LENGTH = "declared_content_length"
    RECEIVED_DECODED_BODY = "received_decoded_body"
