"""SQS本文をJSONとして解析し、工程のイベント契約へ渡す。"""

import json

from app.collection.events import AnalyzableArticleCreatedEvent


class CurationMessageJsonInvalidError(Exception):
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


def parse_analyzable_article_created_event(
    message_body: str,
) -> AnalyzableArticleCreatedEvent:
    """JSON解析後の入力をイベント型の検証入口へ渡す。"""
    if not isinstance(message_body, str):
        raise CurationMessageJsonInvalidError()
    try:
        data = json.loads(
            message_body,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        pass
    else:
        return AnalyzableArticleCreatedEvent.from_input(data)
    # 入力を含む解析例外をcontextに残さないよう、exceptの外で送出する。
    raise CurationMessageJsonInvalidError()
