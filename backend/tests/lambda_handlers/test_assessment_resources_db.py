"""実DBでセッション終了と利用範囲終了時の解放を検証する。"""

from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.lambda_handlers.assessment import resources as module
from app.lambda_handlers.assessment.settings import AssessmentConsumerSettings
from tests.lambda_handlers.iam_fixtures import inject_test_db_signer


@pytest.fixture
def resource_settings(test_database_url, monkeypatch):
    monkeypatch.setattr(
        module, "get_secret_parameter", Mock(return_value=SecretStr("private"))
    )
    return AssessmentConsumerSettings(
        env="test",
        database_url=inject_test_db_signer(monkeypatch, module, test_database_url),
        db_iam_auth=True,
        aws_region="ap-northeast-1",
        deepseek_api_key_parameter_path="/key",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_body", [False, True])
async def test_reuses_connection_rolls_back_and_closes(
    resource_settings, db_session, fail_body
):
    """セッションの正常・異常終了でロールバックし、同じ範囲内では接続を再利用して最後に切断する。"""
    async with module.open_assessment_resources(resource_settings) as resources:
        with (
            pytest.raises(RuntimeError, match="session body failed")
            if fail_body
            else nullcontext()
        ):
            async with resources.session_factory() as session:
                pid = await session.scalar(text("select pg_backend_pid()"))
                assert (
                    await session.scalar(text("show application_name"))
                    == "vector-assessment-consumer"
                )
                await session.execute(
                    text("select set_config('vector.scope_test', 'transaction', true)")
                )
                if fail_body:
                    raise RuntimeError("session body failed")
        async with resources.session_factory() as session:
            assert await session.scalar(text("select pg_backend_pid()")) == pid
            assert (
                await session.scalar(
                    text("select current_setting('vector.scope_test', true)")
                )
                != "transaction"
            )
    assert (
        await db_session.scalar(
            text("select count(*) from pg_stat_activity where pid=:pid"), {"pid": pid}
        )
        == 0
    )
    async with module.open_assessment_resources(resource_settings) as next_resources:
        assert next_resources.session_factory is not resources.session_factory
        async with next_resources.session_factory() as session:
            assert await session.scalar(text("select pg_backend_pid()")) != pid
