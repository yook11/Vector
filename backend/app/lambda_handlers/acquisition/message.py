"""SQS本文をJSONとして解析し、取得依頼の契約へ渡す。"""

import json

from app.collection.sources.acquisition_request import (
    SourceAcquisitionRequest,
    acquisition_request_from_message,
)


class AcquisitionMessageJsonInvalidError(Exception):
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


def parse_acquisition_request(
    message_body: str,
) -> SourceAcquisitionRequest:
    """JSON解析後の入力を取得依頼の検証入口へ渡す。"""
    if not isinstance(message_body, str):
        raise AcquisitionMessageJsonInvalidError()
    try:
        data = json.loads(
            message_body,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        pass
    else:
        return acquisition_request_from_message(data)
    # 入力を含む解析例外をcontextに残さないよう、exceptの外で送出する。
    raise AcquisitionMessageJsonInvalidError()
