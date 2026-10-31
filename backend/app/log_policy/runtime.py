"""目的別ルールをグローバル設定から独立したJSON出力へ接続する。"""

import structlog
from structlog.typing import EventDict, FilteringBoundLogger, WrappedLogger

from app.log_policy.base import LogPolicyRules
from app.log_policy.bound_logger import ApplicationBoundLogger
from app.log_policy.chain import build_processors
from app.log_policy.processor import replaced_log

# default=None で repr による変換を外し、JSON にできない値で例外を投げさせる。
_STRICT_JSON_RENDERER = structlog.processors.JSONRenderer(allow_nan=False, default=None)


def render_policy_json(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> str | bytes:
    """厳しいJSONで出力し、JSONにできないログはログ全体の置き換えを出力する。"""
    try:
        return _STRICT_JSON_RENDERER(logger, method_name, event_dict)
    except Exception:
        replaced = replaced_log(
            event_dict,
            log_policy=event_dict.get("log_policy"),
            reason="json_serialization_failed",
        )
        return _STRICT_JSON_RENDERER(logger, method_name, replaced)


def create_policy_json_logger(name: str, rules: LogPolicyRules) -> FilteringBoundLogger:
    """書き込み時の標準出力へ出すので、モジュールで一度作って使い回せる。"""
    return structlog.wrap_logger(
        structlog.PrintLogger(),
        processors=build_processors(rules, render_policy_json),
        wrapper_class=ApplicationBoundLogger,
        context_class=dict,
        cache_logger_on_first_use=False,
    ).bind()
