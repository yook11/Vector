"""記事単位AI分析のライフサイクルへAssessmentの依存を配線する。"""

from contextlib import AbstractAsyncContextManager

import structlog
from openai import AsyncOpenAI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ai_providers.deepseek.client import open_deepseek_client
from app.ai_providers.deepseek.settings import DeepSeekConnectionSettings
from app.analysis.assessment.ai.deepseek import DeepSeekAssessor
from app.analysis.assessment.ai.spec import DEEPSEEK_ASSESSMENT_SPEC
from app.analysis.assessment.consumer import AssessmentConsumer
from app.db.engine import create_assessment_consumer_engine
from app.lambda_handlers.article_analysis_lifecycle import (
    IamPasswordProvider,
    SessionFactory,
    open_article_analysis_consumer,
)
from app.lambda_handlers.assessment.failure_recorder import (
    AssessmentLambdaFailureRecorder,
)
from app.lambda_handlers.assessment.settings import AssessmentConsumerSettings

logger = structlog.get_logger(__name__)


def open_assessment_consumer(
    settings: AssessmentConsumerSettings,
) -> AbstractAsyncContextManager[AssessmentConsumer]:
    """工程別の生成関数を渡し、資源の準備・終了順序を共通側へ委ねる。"""

    def create_engine(*, password_provider: IamPasswordProvider) -> AsyncEngine:
        return create_assessment_consumer_engine(
            settings, password_provider=password_provider
        )

    def open_client(*, api_key: SecretStr) -> AbstractAsyncContextManager[AsyncOpenAI]:
        return open_deepseek_client(
            api_key=api_key,
            base_url=DEEPSEEK_ASSESSMENT_SPEC.base_url,
            settings=DeepSeekConnectionSettings(),
        )

    def build_consumer(
        *, session_factory: SessionFactory, client: AsyncOpenAI
    ) -> AssessmentConsumer:
        return AssessmentConsumer(session_factory, DeepSeekAssessor(client))

    return open_article_analysis_consumer(
        aws_region=settings.aws_region,
        database_url=settings.database_url,
        api_key_parameter_path=settings.deepseek_api_key_parameter_path,
        create_engine=create_engine,
        open_client=open_client,
        build_consumer=build_consumer,
        failure_recorder=AssessmentLambdaFailureRecorder(logger),
    )
