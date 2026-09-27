"""製品のInsights workerのtaskを、vector_insightsのEngineで動かす。"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.db.engine import create_worker_engine
from app.db.session import caller_managed_session_factory
from local_tests.insights.support import (
    JST,
    BriefingGeneratorStub,
    RecordingNotifier,
)


@pytest.fixture
def insights_settings(monkeypatch, system_database):
    # taskのimportが要求する設定には、テスト専用値だけを渡す。
    for name, value in {
        "ENV": "development",
        "EGRESS_PROXY_URL": "http://proxy.vector.internal:3128",
        "DATABASE_URL": system_database.url("vector_insights", sqlalchemy=True),
        "DB_IAM_AUTH": "false",
        "BFF_JWT_SIGNING_SECRET": "test-insights-bff-signing-key-xxxxxxx",
        "REVALIDATE_BEARER_SECRET": "test-insights-revalidate-xxxxxxxxxx",
        "FRONTEND_URL": "http://localhost:3000",
        "INTERNAL_FRONTEND_BASE_URL": "http://localhost:3000",
        "CROSSREF_CONTACT_EMAIL": "crossref-contact@example.invalid",
    }.items():
        monkeypatch.setenv(name, value)


async def _worker_session_factory(system_database, label):
    engine = create_worker_engine(
        SimpleNamespace(
            database_url=system_database.url("vector_insights", sqlalchemy=True),
            db_iam_auth=False,
            aws_region=None,
        ),
        label,
    )
    async with engine.connect() as connection:
        role = await connection.scalar(text("SELECT current_user"))
    if role != "vector_insights":
        await engine.dispose()
        raise RuntimeError(f"Insights試験がvector_insights以外で接続している: {role}")
    return engine, caller_managed_session_factory(engine)


@pytest.fixture
def notifier(insights_settings, monkeypatch):
    """task が作る通知の送り先を、記録用に置き換える。"""
    from app.shared.revalidate import FrontendRevalidateNotifier

    recording = RecordingNotifier()
    monkeypatch.setattr(
        FrontendRevalidateNotifier,
        "from_settings",
        classmethod(lambda cls, settings: recording),
    )
    return recording


@pytest.fixture
async def trend_worker(system_database, notifier, monkeypatch):
    """trend-discovery workerの資源と、2026-09-27 12:00 JSTに固定した時計。"""
    from app.insights.trend_discovery import service

    monkeypatch.setattr(
        service, "now_in_jst", lambda: datetime(2026, 9, 27, 12, 0, tzinfo=JST)
    )
    engine, session_factory = await _worker_session_factory(
        system_database, "trend_discovery"
    )
    try:
        yield SimpleNamespace(state=SimpleNamespace(session_factory=session_factory))
    finally:
        await engine.dispose()


@pytest.fixture
def briefing_generator():
    return BriefingGeneratorStub()


@pytest.fixture
async def briefing_worker(system_database, notifier, briefing_generator, monkeypatch):
    """briefing workerの資源と、月曜 2026-09-28 00:05 JSTに固定した時計。"""
    from app.queue.tasks import briefing

    monkeypatch.setattr(
        briefing, "now_in_jst", lambda: datetime(2026, 9, 28, 0, 5, tzinfo=JST)
    )
    engine, session_factory = await _worker_session_factory(system_database, "briefing")
    try:
        # 初回の試行として扱い、失敗を再試行の対象に残す。
        yield SimpleNamespace(
            state=SimpleNamespace(
                session_factory=session_factory,
                briefing_generator=briefing_generator,
            ),
            message=SimpleNamespace(labels={"_retries": 0, "max_retries": 2}),
        )
    finally:
        await engine.dispose()


@pytest.fixture
def enqueued_briefings(briefing_worker, monkeypatch):
    """週次の起動が投入するカテゴリ単位のsubtaskを記録する。"""
    from app.queue.tasks import briefing

    enqueued = []

    async def record(task_input):
        enqueued.append(task_input)

    monkeypatch.setattr(briefing.generate_briefing_for_category, "kiq", record)
    return enqueued
