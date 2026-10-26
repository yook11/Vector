"""SQSトリガーへ返すLambdaの部分バッチ応答の形式。"""

from collections.abc import Sequence
from typing import TypedDict


class BatchItemFailure(TypedDict):
    """部分バッチ応答で、削除させないメッセージ1件を受信したmessageIdで示す。"""

    itemIdentifier: str


class PartialBatchResponse(TypedDict):
    """載せたメッセージは削除されずに再配信され、載せなかったメッセージは処理済みとして削除される。"""

    batchItemFailures: list[BatchItemFailure]


def partial_batch_response(message_ids: Sequence[str]) -> PartialBatchResponse:
    """列挙したメッセージだけを再配信させ、残りは処理済みとして削除させる。"""
    return PartialBatchResponse(
        batchItemFailures=[
            BatchItemFailure(itemIdentifier=message_id) for message_id in message_ids
        ]
    )
