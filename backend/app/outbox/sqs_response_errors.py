"""SQS応答検証とエラーマッピングで共有する、理由付きの応答違反。"""

from app.outbox.publish_errors import PublishResponseField, PublishResponseInvalidReason


class InvalidSqsBatchResponse(ValueError):
    """応答の値を保持せず、違反理由と不正箇所だけを伝える。"""

    def __init__(
        self, *, reason: PublishResponseInvalidReason, field: PublishResponseField
    ) -> None:
        super().__init__()
        self.reason = reason
        self.field = field
