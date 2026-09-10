from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from app.db.settings import DatabaseConnectionSettings


class OutboxRelaySettings(DatabaseConnectionSettings):
    """relayに必要な接続設定のみを環境変数から読み込む。"""

    model_config = SettingsConfigDict(env_file=None, hide_input_in_errors=True)

    env: Literal["development", "test", "production"] = "production"
    aws_region: str = Field(min_length=1)
    sqs_article_completion_queue_url: str = Field(min_length=1)
    sqs_article_curation_queue_url: str = Field(min_length=1)
    sqs_article_assessment_queue_url: str = Field(min_length=1)
    sqs_article_embedding_queue_url: str = Field(min_length=1)
