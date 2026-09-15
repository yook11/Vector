"""Schedulerの予定回を検証し、取得依頼の投入へ接続する。"""

import asyncio

from app.collection.sources.acquisition_request import SourceAcquisitionSchedule
from app.lambda_handlers.logging import setup_lambda_logging
from app.lambda_handlers.source_dispatch.failure import (
    FailurePhase,
    SourceDispatchLambdaError,
    record_failure,
)
from app.lambda_handlers.source_dispatch.resources import open_source_dispatcher
from app.lambda_handlers.source_dispatch.settings import SourceDispatchSettings


def handler(schedule_input: object, context: object) -> None:
    setup_lambda_logging()
    phase: FailurePhase = "input"
    try:
        schedule = SourceAcquisitionSchedule.model_validate(schedule_input)
        phase = "settings"
        settings = SourceDispatchSettings()  # type: ignore[call-arg]
        phase = "dispatch"
        asyncio.run(run_source_dispatch(schedule, settings))
    except SourceDispatchLambdaError:
        raise
    except Exception as exc:
        raise record_failure(phase, exc) from None


async def run_source_dispatch(
    schedule: SourceAcquisitionSchedule, settings: SourceDispatchSettings
) -> None:
    phase: FailurePhase = "resources"
    try:
        async with open_source_dispatcher(settings) as dispatcher:
            phase = "dispatch"
            await dispatcher.dispatch(schedule)
    except Exception as exc:
        raise record_failure(phase, exc) from None
