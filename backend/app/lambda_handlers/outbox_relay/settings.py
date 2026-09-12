from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from app.db.settings import DatabaseConnectionSettings


class OutboxRelayConnectionSettings(DatabaseConnectionSettings):
    """relayに必要な接続設定のみを環境変数から読み込む。"""

    model_config = SettingsConfigDict(env_file=None, hide_input_in_errors=True)

    env: Literal["development", "test", "production"] = "production"
    aws_region: str = Field(min_length=1)


class EmbeddingOutboxRelaySettings(OutboxRelayConnectionSettings):
    """Embedding配送に必要なキューだけを要求する。"""

    sqs_article_embedding_queue_url: str = Field(min_length=1)


class AssessmentOutboxRelaySettings(OutboxRelayConnectionSettings):
    """Assessment配送に必要なキューだけを要求する。"""

    sqs_article_assessment_queue_url: str = Field(min_length=1)
