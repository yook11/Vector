"""記事単位AI分析のLambda呼び出しの相関情報・ロガー・設定を用意し、終了時に戻す。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

import structlog
from structlog.typing import FilteringBoundLogger

from app.analysis.logging import create_article_analysis_logger
from app.lambda_handlers.article_analysis_lifecycle import (
    ArticleAnalysisLifecycleRecorder,
)


class ArticleAnalysisSettings(Protocol):
    @property
    def env(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ArticleAnalysisInvocation[SettingsT: ArticleAnalysisSettings]:
    """1回の呼び出しで共有する設定・ロガー・初期化失敗の記録先。"""

    settings: SettingsT
    logger: FilteringBoundLogger
    failure_recorder: ArticleAnalysisLifecycleRecorder


@contextmanager
def open_article_analysis_invocation[SettingsT: ArticleAnalysisSettings](
    stage: str,
    context: object,
    load_settings: Callable[[], SettingsT],
) -> Iterator[ArticleAnalysisInvocation[SettingsT]]:
    """呼び出しの相関情報をcontextvarsに置き、呼び出し元の文脈は終了時に戻す。"""
    outer_context = structlog.contextvars.get_contextvars()
    structlog.contextvars.clear_contextvars()
    try:
        structlog.contextvars.bind_contextvars(service="article_analysis", stage=stage)
        request_id = getattr(context, "aws_request_id", None)
        if isinstance(request_id, str) and request_id.strip():
            structlog.contextvars.bind_contextvars(request_id=request_id)
        logger = create_article_analysis_logger()
        failure_recorder = ArticleAnalysisLifecycleRecorder(logger, stage=stage)
        try:
            settings = load_settings()
        except Exception as exc:
            failure_recorder.record_initialization_failure("settings", exc)
            raise
        structlog.contextvars.bind_contextvars(environment=settings.env)
        yield ArticleAnalysisInvocation(settings, logger, failure_recorder)
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**outer_context)
