"""工程を問わず共有する、migration適用済みDBのfixture。"""

import pytest

from local_tests.database import isolated_database, migrated_database


@pytest.fixture
def analysis_engines(monkeypatch):
    """実ConsumerがAIを待つ間のセッション返却を観測する。"""
    from app.lambda_handlers import article_analysis_lifecycle

    engines = []
    create = article_analysis_lifecycle.caller_managed_session_factory

    def observe(engine):
        engines.append(engine)
        return create(engine)

    monkeypatch.setattr(
        article_analysis_lifecycle, "caller_managed_session_factory", observe
    )
    return engines


@pytest.fixture(scope="session")
def system_database_template():
    with migrated_database() as database:
        yield database


@pytest.fixture
async def system_database(system_database_template):
    async with isolated_database(system_database_template) as database:
        yield database


def pytest_sessionfinish(session, exitstatus):
    """必須のローカルテストをskipした実行は合格にしない。"""
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    if reporter is not None and reporter.stats.get("skipped"):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
