"""記事単位AI分析のライフサイクルへCurationの依存を配線する。"""

from contextlib import AbstractAsyncContextManager

import structlog
from google.genai.client import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from app.ai_providers.gemini.client import open_gemini_client
from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.analysis.curation.ai.gemini import GeminiCurator
from app.analysis.curation.consumer import CurationConsumer
from app.db.engine import create_curation_consumer_engine
from app.lambda_handlers.article_analysis_lifecycle import (
    IamPasswordProvider,
    SessionFactory,
    open_article_analysis_consumer,
)
from app.lambda_handlers.curation.failure_recorder import (
    CurationLambdaFailureRecorder,
)
from app.lambda_handlers.curation.settings import CurationConsumerSettings

logger = structlog.get_logger(__name__)


def open_curation_consumer(
    settings: CurationConsumerSettings,
) -> AbstractAsyncContextManager[CurationConsumer]:
    """工程別の生成関数を渡し、資源の準備・終了順序を共通側へ委ねる。"""

    def create_engine(*, password_provider: IamPasswordProvider) -> AsyncEngine:
        return create_curation_consumer_engine(
            settings, password_provider=password_provider
        )

    def open_client(*, api_key: SecretStr) -> AbstractAsyncContextManager[AsyncClient]:
        return open_gemini_client(
            api_key=api_key,
            settings=GeminiConnectionSettings(),
        )

    def build_consumer(
        *, session_factory: SessionFactory, client: AsyncClient
    ) -> CurationConsumer:
        return CurationConsumer(session_factory, GeminiCurator(client=client))

    return open_article_analysis_consumer(
        aws_region=settings.aws_region,
        database_url=settings.database_url,
        api_key_parameter_path=settings.gemini_api_key_parameter_path,
        create_engine=create_engine,
        open_client=open_client,
        build_consumer=build_consumer,
        failure_recorder=CurationLambdaFailureRecorder(logger),
    )
