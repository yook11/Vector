"""Lambdaのアプリログを全体設定に依存せずJSON標準出力へ接続する。"""

import logging

import structlog


def setup_lambda_logging() -> None:
    """各呼び出しの設定検証前に、同じJSON出力設定を適用する。"""
    try:
        structlog.configure(
            processors=[
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
            context_class=dict,
            logger_factory=structlog.WriteLoggerFactory(),
            cache_logger_on_first_use=False,
        )
    except Exception:  # noqa: S110
        # 診断の初期化障害で業務処理や元の例外を置き換えない。
        pass
