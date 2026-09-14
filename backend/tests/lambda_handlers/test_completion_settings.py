"""補完の待機先は本文ではなく必須設定として確定する。"""

import pytest
from pydantic import ValidationError

from app.lambda_handlers.completion.settings import CompletionConsumerSettings


@pytest.mark.parametrize("value", [None, "", " \t"])
def test_invalid_queue_setting_prevents_startup(value):
    """欠落・空のキュー設定では起動しない。"""
    with pytest.raises(ValidationError):
        CompletionConsumerSettings(
            database_url="postgresql+asyncpg://vector_collect@db.invalid/vector?sslmode=require",
            aws_region="ap-northeast-1",
            sqs_article_completion_queue_url=value,
        )


def test_missing_queue_setting_prevents_startup(monkeypatch):
    """環境にもキュー設定がなければ既定の配送先へ代替しない。"""
    monkeypatch.delenv("SQS_ARTICLE_COMPLETION_QUEUE_URL", raising=False)
    with pytest.raises(ValidationError) as caught:
        CompletionConsumerSettings(
            database_url="postgresql+asyncpg://vector_collect@db.invalid/vector?sslmode=require",
            aws_region="ap-northeast-1",
        )
    assert any(
        e["loc"] == ("sqs_article_completion_queue_url",) and e["type"] == "missing"
        for e in caught.value.errors()
    )
