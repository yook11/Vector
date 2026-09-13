"""実handler・共通資源管理・SDK・ConsumerからDBまでの接続を検証する。"""

import asyncio
import json
from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from structlog.testing import capture_logs

from app.ai_providers.gemini import client as gemini_client
from app.analysis.curation.domain.ready import CurationReadyBuildRejectionReason
from app.analysis.curation.events import ArticleCuratedSignal
from app.lambda_handlers import article_analysis_lifecycle as lifecycle
from app.lambda_handlers.curation import composition
from app.lambda_handlers.curation.settings import CurationConsumerSettings
from app.models.analyzable_article_record import AnalyzableArticleRecord
from app.models.article_curation import ArticleCuration
from app.models.curation_noise import CurationNoise
from app.models.outbox_event import OutboxEvent
from app.models.pipeline_event import PipelineEvent
from tests.iam_fixtures import inject_test_db_signer
from tests.lambda_handlers.test_curation_handler import valid_body

module = import_module("app.lambda_handlers.curation.handler")
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def runtime(monkeypatch, test_database_url):
    state = SimpleNamespace(
        outcomes=[],
        requests=[],
        http_clients=[],
        engines=[],
        rds=[],
        cleanup_failure=False,
    )
    settings = CurationConsumerSettings(
        env="test",
        database_url=inject_test_db_signer(
            monkeypatch, test_database_url, resources_module=lifecycle
        ),
        db_iam_auth=True,
        aws_region="ap-northeast-1",
        gemini_api_key_parameter_path="/test/curation/gemini-key",
    )
    state.settings = settings
    monkeypatch.setattr(module, "CurationConsumerSettings", Mock(return_value=settings))
    monkeypatch.setattr(module, "setup_lambda_logging", Mock())
    monkeypatch.setattr(
        lifecycle, "get_secret_parameter", Mock(return_value=SecretStr("test-key"))
    )
    create_rds = lifecycle.Session().create_client.side_effect

    def track_rds(*args, **kwargs):
        rds = create_rds(*args, **kwargs)
        if state.cleanup_failure:
            rds.close.side_effect = RuntimeError("private-rds-close")
        state.rds.append(rds)
        return rds

    lifecycle.Session().create_client.side_effect = track_rds
    create_engine = composition.create_curation_consumer_engine

    def track_engine(*args, **kwargs):
        engine = create_engine(*args, **kwargs)
        state.engines.append(engine)
        return engine

    monkeypatch.setattr(composition, "create_curation_consumer_engine", track_engine)
    dispose_engine = lifecycle._dispose_engine

    async def observe_dispose(dispose, recorder):
        async def close():
            await dispose()
            if state.cleanup_failure:
                raise RuntimeError("private-engine-close")

        await dispose_engine(close, recorder)

    state.dispose = AsyncMock(side_effect=observe_dispose)
    monkeypatch.setattr(lifecycle, "_dispose_engine", state.dispose)

    def respond(request):
        assert state.engines[-1].pool.checkedout() == 0
        state.requests.append(request)
        outcome = state.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "timeout":
            raise httpx.ReadTimeout("private-ai-timeout", request=request)
        if outcome == "blocked":
            return httpx.Response(
                200, json={"candidates": [{"finishReason": "SAFETY"}]}
            )
        body = json.dumps(
            {"relevance": outcome, "title_ja": "タイトル", "summary_ja": "要約"}
        )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": body}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    def open_http(**kwargs):
        assert kwargs.pop("retries") == 0
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond), **kwargs)
        if state.cleanup_failure:
            close = client.aclose

            async def fail_close():
                await close()
                raise RuntimeError("private-http-close")

            client.aclose = fail_close
        state.http_clients.append(client)
        return client

    monkeypatch.setattr(gemini_client, "make_external_async_client", open_http)
    return state


async def article(db_session, source, name, *, content="content"):
    record = AnalyzableArticleRecord(
        source_id=source.id,
        source_url=f"https://example.com/lambda/{name}",
        original_title=name,
        original_content=content,
        published_at=datetime.now(UTC),
    )
    db_session.add(record)
    await db_session.commit()
    return record.id


def message(name, article_id):
    return {"messageId": name, "body": valid_body(analyzable_article_id=article_id)}


async def rows(session, model):
    return list((await session.scalars(select(model))).all())


def assert_closed(runtime, *, invocations=1):
    assert (
        len(runtime.http_clients)
        == len(runtime.engines)
        == len(runtime.rds)
        == invocations
    )
    assert all(client.is_closed for client in runtime.http_clients)
    assert runtime.dispose.await_count == invocations
    for rds in runtime.rds:
        rds.close.assert_called_once()
        assert rds.generate_db_auth_token.call_count >= 1


@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_mixed_batch_matches_persistence_and_releases_resources(
    runtime, db_session, sample_source, cleanup_failure
):
    runtime.cleanup_failure = cleanup_failure
    ids = {
        name: await article(db_session, sample_source, name)
        for name in ("signal", "noise", "failed", "after")
    }
    ids["large"] = await article(
        db_session, sample_source, "large", content="秘" * 200_001
    )
    ids["invalid"] = await article(db_session, sample_source, "invalid", content="")
    runtime.outcomes = ["signal", "noise", "timeout", "signal"]
    batch = {
        "Records": [
            message("signal", ids["signal"]),
            message("noise", ids["noise"]),
            message("already-signal", ids["signal"]),
            message("already-noise", ids["noise"]),
            message("missing", 999_999),
            message("large", ids["large"]),
            message("invalid", ids["invalid"]),
            {"messageId": "bad-event", "body": "private-invalid-json"},
            message("failed", ids["failed"]),
            message("after", ids["after"]),
        ]
    }
    with capture_logs() as logs:
        response = await asyncio.to_thread(module.handler, batch, None)
    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": "bad-event"},
            {"itemIdentifier": "failed"},
        ]
    }
    assert len(runtime.requests) == 4
    assert len(await rows(db_session, AnalyzableArticleRecord)) == 6
    assert {
        row.analyzable_article_id for row in await rows(db_session, ArticleCuration)
    } == {ids["signal"], ids["after"]}
    assert {
        row.analyzable_article_id for row in await rows(db_session, CurationNoise)
    } == {ids["noise"]}
    outbox = await rows(db_session, OutboxEvent)
    assert len(outbox) == 2
    assert {row.event_type for row in outbox} == {ArticleCuratedSignal.EVENT_TYPE}
    assert {row.payload["analyzable_article_id"] for row in outbox} == {
        ids["signal"],
        ids["after"],
    }
    audits = await rows(db_session, PipelineEvent)
    assert len(audits) == 7
    rejected = {
        row.payload["target_article_id"]: row
        for row in audits
        if row.event_type == "rejected"
    }
    assert rejected[999_999].article_id is None
    for target, reason in [
        (999_999, CurationReadyBuildRejectionReason.ARTICLE_MISSING),
        (ids["large"], CurationReadyBuildRejectionReason.CONTENT_TOO_LARGE),
        (ids["invalid"], CurationReadyBuildRejectionReason.INPUT_INVALID),
    ]:
        assert rejected[target].outcome_code == reason.value
        if target != 999_999:
            assert rejected[target].article_id == target
    assert [
        (row.article_id, row.event_type) for row in audits if row.event_type == "failed"
    ] == [(ids["failed"], "failed")]
    completions = {
        log["message_id"]: log
        for log in logs
        if log["event"] == "curation_message_completed"
    }
    assert completions["signal"]["reason"] == "signal"
    assert completions["noise"]["reason"] == "noise"
    assert (
        completions["already-signal"]["reason"]
        == completions["already-noise"]["reason"]
        == "already_curated"
    )
    assert completions["large"]["reason"] == "ready_build_rejected"
    assert (
        completions["large"]["rejection_code"]
        == CurationReadyBuildRejectionReason.CONTENT_TOO_LARGE.value
    )
    assert "private-" not in repr(logs)
    assert_closed(runtime)
    with capture_logs():
        again = await asyncio.to_thread(
            module.handler, {"Records": [message("again", ids["signal"])]}, None
        )
    assert again == {"batchItemFailures": []}
    assert len(runtime.requests) == 4
    assert len(await rows(db_session, PipelineEvent)) == 7
    assert_closed(runtime, invocations=2)
    assert runtime.http_clients[0] is not runtime.http_clients[1]
    assert runtime.engines[0] is not runtime.engines[1]


async def test_outbox_failure_rolls_back_signal_and_continues_noise(
    runtime, db_session, sample_source, reject_outbox_insert
):
    try:
        await reject_outbox_insert(ArticleCuratedSignal.EVENT_TYPE)
        signal_id = await article(db_session, sample_source, "signal")
        noise_id = await article(db_session, sample_source, "noise")
        runtime.outcomes = ["signal", "noise"]
        response = await asyncio.to_thread(
            module.handler,
            {"Records": [message("failed", signal_id), message("noise", noise_id)]},
            None,
        )
        assert response == {"batchItemFailures": [{"itemIdentifier": "failed"}]}
        assert await rows(db_session, ArticleCuration) == []
        assert await rows(db_session, OutboxEvent) == []
        assert len(await rows(db_session, CurationNoise)) == 1
        audits = await rows(db_session, PipelineEvent)
        assert {(row.article_id, row.event_type) for row in audits} == {
            (signal_id, "failed"),
            (noise_id, "succeeded"),
        }
        assert len(await rows(db_session, AnalyzableArticleRecord)) == 2
        assert_closed(runtime)
    finally:
        # 検証用SELECTのロックを、障害注入制約の削除より先に解放する。
        await db_session.rollback()


async def test_provider_content_refusal_is_retryable_at_lambda_without_article_deletion(
    runtime, db_session, sample_source
):
    target = await article(db_session, sample_source, "blocked")
    runtime.outcomes = ["blocked"]
    response = await asyncio.to_thread(
        module.handler, {"Records": [message("blocked", target)]}, None
    )
    assert response == {"batchItemFailures": [{"itemIdentifier": "blocked"}]}
    assert await db_session.get(AnalyzableArticleRecord, target) is not None
    assert await rows(db_session, ArticleCuration) == []
    assert await rows(db_session, OutboxEvent) == []
    (audit,) = await rows(db_session, PipelineEvent)
    assert (audit.event_type, audit.article_id) == ("failed", target)
    assert_closed(runtime)


async def test_cancellation_propagates_after_releasing_invocation_resources(
    runtime, db_session, sample_source
):
    target = await article(db_session, sample_source, "cancelled")
    original = asyncio.CancelledError()
    runtime.outcomes = [original]
    with pytest.raises(asyncio.CancelledError):
        await asyncio.to_thread(
            module.handler,
            {"Records": [message("cancelled", target), message("unprocessed", target)]},
            None,
        )
    assert len(runtime.requests) == 1
    assert await rows(db_session, ArticleCuration) == []
    assert await rows(db_session, PipelineEvent) == []
    assert_closed(runtime)
