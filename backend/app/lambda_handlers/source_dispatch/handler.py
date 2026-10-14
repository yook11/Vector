"""Schedulerの予定回を検証し、取得依頼の投入へ接続する。"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter

import structlog

from app.collection.sources.acquisition_request import SourceAcquisitionSchedule
from app.lambda_handlers.source_dispatch.recorder import (
    DispatchOperation,
    SourceDispatchRecorder,
)
from app.lambda_handlers.source_dispatch.resources import open_source_dispatcher
from app.lambda_handlers.source_dispatch.settings import SourceDispatchSettings
from app.log_policy.policies.pipeline_control import PIPELINE_CONTROL_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger
from app.shared.time import elapsed_ms_since


def handler(schedule_input: object, context: object) -> None:
    with _open_invocation(context) as recorder:
        started_at_seconds = perf_counter()
        operation: DispatchOperation = "validate_event"
        try:
            schedule = SourceAcquisitionSchedule.model_validate(schedule_input)
            structlog.contextvars.bind_contextvars(**schedule.model_dump(mode="json"))
            operation = "settings"
            settings = SourceDispatchSettings()  # type: ignore[call-arg]
        except Exception as exc:
            recorder.record_failed(
                operation, exc, duration_ms=elapsed_ms_since(started_at_seconds)
            )
            raise RuntimeError(f"source_dispatch_{operation}_failed") from None
        structlog.contextvars.bind_contextvars(environment=settings.env)
        asyncio.run(
            _run(
                schedule,
                settings,
                recorder=recorder,
                started_at_seconds=started_at_seconds,
            )
        )


@contextmanager
def _open_invocation(context: object) -> Iterator[SourceDispatchRecorder]:
    """呼び出しの相関情報をcontextvarsに置き、呼び出し元の文脈は終了時に戻す。"""
    outer_context = structlog.contextvars.get_contextvars()
    structlog.contextvars.clear_contextvars()
    try:
        structlog.contextvars.bind_contextvars(
            service="article_fetch", stage="dispatch"
        )
        request_id = getattr(context, "aws_request_id", None)
        if isinstance(request_id, str) and request_id.strip():
            structlog.contextvars.bind_contextvars(request_id=request_id)
        yield SourceDispatchRecorder(
            create_policy_json_logger("article_fetch", PIPELINE_CONTROL_LOG_RULES)
        )
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**outer_context)


async def _run(
    schedule: SourceAcquisitionSchedule,
    settings: SourceDispatchSettings,
    *,
    recorder: SourceDispatchRecorder,
    started_at_seconds: float,
) -> None:
    operation: DispatchOperation = "resources"
    try:
        async with open_source_dispatcher(settings, recorder=recorder) as dispatcher:
            operation = "dispatch"
            sent_count = await dispatcher.dispatch(schedule)
    except Exception as exc:
        recorder.record_failed(
            operation, exc, duration_ms=elapsed_ms_since(started_at_seconds)
        )
        raise RuntimeError(f"source_dispatch_{operation}_failed") from None
    recorder.record_completed(
        sent_count, duration_ms=elapsed_ms_since(started_at_seconds)
    )
