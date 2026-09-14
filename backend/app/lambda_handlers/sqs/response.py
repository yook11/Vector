"""SQS受信工程で共有する部分バッチ応答の形式。"""

from typing import TypedDict


class SqsBatchItemIdentifier(TypedDict):
    itemIdentifier: str


class SqsBatchFailureResponse(TypedDict):
    batchItemFailures: list[SqsBatchItemIdentifier]
