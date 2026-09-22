"""工程別backfillが必要とする接続と有効設定を読み込む。"""

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from app.db.settings import DatabaseConnectionSettings


class BackfillConnectionSettings(DatabaseConnectionSettings):
    model_config = SettingsConfigDict(env_file=None, hide_input_in_errors=True)

    env: Literal["development", "test", "production"] = "production"
    aws_region: str = Field(min_length=1)
    database_url: str = Field(repr=False)
    db_iam_auth: bool = True

    @field_validator("aws_region")
    @classmethod
    def _reject_blank_region(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("AWS region must not be blank")
        return value

    @field_validator(
        "sqs_article_curation_queue_url",
        "sqs_article_assessment_queue_url",
        "sqs_article_embedding_queue_url",
        "sqs_article_completion_queue_url",
        check_fields=False,
    )
    @classmethod
    def _reject_blank_queue(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("queue URL must not be blank")
        return value

    @model_validator(mode="after")
    def _require_iam(self) -> Self:
        if not self.db_iam_auth:
            raise ValueError("Backfill requires RDS IAM authentication")
        return self


class CurationBackfillSettings(BackfillConnectionSettings):
    sqs_article_curation_queue_url: str = Field(min_length=1)
    backfill_curations_enabled: bool = True


class AssessmentBackfillSettings(BackfillConnectionSettings):
    sqs_article_assessment_queue_url: str = Field(min_length=1)
    backfill_assessments_enabled: bool = True


class EmbeddingBackfillSettings(BackfillConnectionSettings):
    sqs_article_embedding_queue_url: str = Field(min_length=1)
    backfill_embeddings_enabled: bool = True


class CompletionBackfillSettings(BackfillConnectionSettings):
    sqs_article_completion_queue_url: str = Field(min_length=1)
    backfill_completions_enabled: bool = True
