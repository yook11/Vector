"""SQS受信工程で共有する部分バッチ応答の形式。"""

from collections.abc import Sequence
from typing import TypedDict


class RedeliveryResponse(TypedDict):
    """AWSの部分バッチ応答の形式で、再配信させるメッセージをSQSトリガーへ伝える。"""

    batchItemFailures: list[dict[str, str]]


def redelivery_response(message_ids: Sequence[str]) -> RedeliveryResponse:
    """列挙したメッセージだけを再配信させ、残りは処理済みとして削除させる。"""
    return RedeliveryResponse(
        batchItemFailures=[{"itemIdentifier": message_id} for message_id in message_ids]
    )
