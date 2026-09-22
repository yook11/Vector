"""記事単位AI分析のライフサイクルへAssessmentの依存を配線する。"""

import asyncio
from contextlib import AbstractAsyncContextManager

from openai import AsyncOpenAI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.typing import FilteringBoundLogger

from app.ai_providers.deepseek.client import open_deepseek_client
from app.ai_providers.deepseek.settings import DeepSeekConnectionSettings
from app.analysis.assessment.ai.deepseek import DeepSeekAssessor
from app.analysis.assessment.ai.spec import DEEPSEEK_ASSESSMENT_SPEC
from app.analysis.assessment.consumer import AssessmentConsumer
from app.analysis.assessment.repository import AssessmentRepository
from app.aws.ssm import get_secret_parameter
from app.db.engine import create_assessment_consumer_engine
from app.lambda_handlers.article_analysis_lifecycle import (
    IamPasswordProvider,
    SessionFactory,
    open_article_analysis_consumer,
)
from app.lambda_handlers.assessment.failure_recorder import (
    AssessmentLambdaFailureRecorder,
)
from app.lambda_handlers.assessment.notification import ArticleListUpdateNotifier
from app.lambda_handlers.assessment.settings import (
    AssessmentConsumerSettings,
    AssessmentNotificationSettings,
)
from app.shared.revalidate import FrontendRevalidateNotifier


def open_assessment_consumer(
    settings: AssessmentConsumerSettings,
    *,
    logger: FilteringBoundLogger,
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
            logger=logger,
        )

    async def build_consumer(
        *, session_factory: SessionFactory, client: AsyncOpenAI
    ) -> AssessmentConsumer:
        async with session_factory() as session:
            await AssessmentRepository(session).assert_category_catalog_covers_enum()
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


def build_article_list_notifier(*, aws_region: str) -> ArticleListUpdateNotifier:
    """通知先を先に確定し、認証キーは保存後の通知時に取得する。"""
    settings = AssessmentNotificationSettings()  # type: ignore[call-arg]

    async def secret_provider() -> SecretStr:
        return await asyncio.to_thread(
            get_secret_parameter,
            region=aws_region,
            path=settings.revalidate_bearer_secret_parameter_path,
        )

    transport = FrontendRevalidateNotifier(
        frontend_base_url=settings.internal_frontend_base_url,
        secret_provider=secret_provider,
    )
    return ArticleListUpdateNotifier(transport)
