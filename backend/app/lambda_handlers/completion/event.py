"""SQS本文をJSONとして解析し、工程のイベント契約へ渡す。"""

import json

from app.collection.article_acquisition.events import IncompleteArticleRecordedEvent


class CompletionMessageJsonInvalidError(Exception):
    """SQS本文をJSONとして解析できない。"""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("nonstandard_json_constant")


def parse_incomplete_article_recorded_event(
    message_body: str,
) -> IncompleteArticleRecordedEvent:
    """JSON解析後の入力をイベント型の検証入口へ渡す。"""
    if not isinstance(message_body, str):
        raise CompletionMessageJsonInvalidError()
    try:
        data = json.loads(
            message_body,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        pass
    else:
        return IncompleteArticleRecordedEvent.from_input(data)
    # 入力を含む解析例外をcontextに残さないよう、exceptの外で送出する。
    raise CompletionMessageJsonInvalidError()
