"""呼び出しの利用範囲に合わせてAssessmentConsumerを組み立てる。"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import structlog

from app.ai_providers.deepseek.client import open_deepseek_client
from app.ai_providers.deepseek.settings import DeepSeekConnectionSettings
from app.analysis.assessment.ai.deepseek import DeepSeekAssessor
from app.analysis.assessment.ai.spec import DEEPSEEK_ASSESSMENT_SPEC
from app.analysis.assessment.consumer import AssessmentConsumer
from app.lambda_handlers.assessment.failure_recorder import (
    AssessmentLambdaFailureRecorder,
)
from app.lambda_handlers.assessment.resources import open_assessment_resources
from app.lambda_handlers.assessment.settings import AssessmentConsumerSettings

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def open_assessment_consumer(
    settings: AssessmentConsumerSettings,
) -> AsyncIterator[AssessmentConsumer]:
    """準備済みConsumerを借用させ、利用終了後に所有資源を解放する。"""
    async with AsyncExitStack() as stack:
        stage = "resources"
        try:
            resources = await stack.enter_async_context(
                open_assessment_resources(settings)
            )
            stage = "deepseek_client"
            client = await stack.enter_async_context(
                open_deepseek_client(
                    api_key=resources.deepseek_api_key,
                    base_url=DEEPSEEK_ASSESSMENT_SPEC.base_url,
                    settings=DeepSeekConnectionSettings(),
                )
            )
            stage = "consumer"
            consumer = AssessmentConsumer(
                resources.session_factory, DeepSeekAssessor(client)
            )
        except Exception as exc:
            AssessmentLambdaFailureRecorder(logger).record_initialization_failure(
                stage, exc
            )
            raise
        yield consumer
