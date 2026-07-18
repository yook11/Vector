"""brokers.py の composition root と worker runtime 設定に関するテスト。"""

import configparser
import importlib
import inspect
import re
import shlex
from pathlib import Path
from typing import Any, get_type_hints
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from structlog.testing import capture_logs
from taskiq import TaskiqEvents, TaskiqState

import app.db_ssl as db_ssl
from app.collection.sources.fetch_cadence import FetchCadence
from app.queue.lifecycle import (
    WORKER_POOL_RECYCLE_SECONDS,
    WORKER_POOL_SIZING,
    build_worker_engine,
    worker_service_name,
)
from app.queue.messages.collection import AcquireSourceTaskInput
from app.queue.schedule import CADENCE_CRON

# supervisord の worker 定義 (taskiq worker 起動引数の SSoT)。
_SUPERVISORD_DIR = Path(__file__).resolve().parent.parent / "supervisord"


def _analysis_worker_commands() -> list[list[str]]:
    """``broker_analysis`` を起動する worker command を token 列で返す。"""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(_SUPERVISORD_DIR / "analysis.conf")
    return [
        shlex.split(command)
        for section in parser.sections()
        if section.startswith("program:")
        and "taskiq worker" in (command := parser[section].get("command", ""))
        and "app.queue.brokers:broker_analysis" in command
    ]


def _maintenance_worker_commands() -> list[list[str]]:
    """``broker_maintenance`` を起動するworker commandをtoken列で返す。"""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(_SUPERVISORD_DIR / "analysis.conf")
    return [
        shlex.split(command)
        for section in parser.sections()
        if section.startswith("program:")
        and "taskiq worker" in (command := parser[section].get("command", ""))
        and "app.queue.brokers:broker_maintenance" in command
    ]


def _fetch_worker_commands() -> dict[str, list[str]]:
    """fetch container の worker program を program 名から command token 列へ写す。"""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(_SUPERVISORD_DIR / "fetch.conf")
    return {
        section.removeprefix("program:"): shlex.split(command)
        for section in parser.sections()
        if section.startswith("program:")
        and "taskiq worker" in (command := parser[section].get("command", ""))
    }


def _parse_worker_programs() -> dict[str, int | None]:
    """supervisord ``*.conf`` の worker を ``{label: max_async_tasks}`` に解す。

    ``label`` は ``broker_<label>`` から取り、``--max-async-tasks`` 不在は
    ``None`` (明示漏れ) を返す。``taskiq scheduler`` / eventlistener は worker でない
    ため対象外。
    """
    workers: dict[str, int | None] = {}
    for conf in sorted(_SUPERVISORD_DIR.glob("*.conf")):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(conf)
        for section in parser.sections():
            if not section.startswith("program:"):
                continue
            command = parser[section].get("command", "")
            if "taskiq worker" not in command:
                continue
            label_match = re.search(r"broker_(\w+)", command)
            assert label_match, f"{section}: broker module not found in command"
            max_match = re.search(r"--max-async-tasks\s+(\d+)", command)
            workers[label_match.group(1)] = (
                int(max_match.group(1)) if max_match else None
            )
    return workers


def test_analysis_broker_reads_only_stage_specific_streams() -> None:
    """analysis broker は curation を主 Stream、assessment を追加購読に固定する。"""
    from app.queue.brokers import broker_analysis

    assert {
        "queue_name": broker_analysis.queue_name,
        "additional_streams": broker_analysis.additional_streams,
        "consumer_group_name": broker_analysis.consumer_group_name,
        "consumer_id": broker_analysis.consumer_id,
        "maxlen": broker_analysis.maxlen,
        "idle_timeout": broker_analysis.idle_timeout,
        "unacknowledged_batch_size": broker_analysis.unacknowledged_batch_size,
        "unacknowledged_lock_timeout": broker_analysis.unacknowledged_lock_timeout,
    } == {
        "queue_name": "pipeline:curation",
        "additional_streams": {"pipeline:assessment": ">"},
        "consumer_group_name": "taskiq",
        "consumer_id": "0-0",
        "maxlen": 10_000,
        "idle_timeout": 600_000,
        "unacknowledged_batch_size": 100,
        "unacknowledged_lock_timeout": 60,
    }


def test_collection_broker_reads_only_stage_specific_streams() -> None:
    """content broker は acquisition を主 Stream、completion だけを追加購読する。"""
    from app.queue.brokers import broker_content

    assert {
        "queue_name": broker_content.queue_name,
        "additional_streams": broker_content.additional_streams,
        "consumer_group_name": broker_content.consumer_group_name,
        "consumer_id": broker_content.consumer_id,
        "maxlen": broker_content.maxlen,
        "unacknowledged_batch_size": broker_content.unacknowledged_batch_size,
        "unacknowledged_lock_timeout": broker_content.unacknowledged_lock_timeout,
    } == {
        "queue_name": "pipeline:acquisition",
        "additional_streams": {"pipeline:completion": ">"},
        "consumer_group_name": "taskiq",
        "consumer_id": "0-0",
        "maxlen": 10_000,
        "unacknowledged_batch_size": 100,
        "unacknowledged_lock_timeout": 60,
    }


@pytest.mark.parametrize(
    (
        "task_module",
        "task_attr",
        "expected_task_name",
        "expected_labels",
        "expected_payload",
    ),
    [
        (
            "app.queue.tasks.acquisition",
            "acquire_source",
            "acquire_source",
            {
                "queue_name": "pipeline:acquisition",
                "timeout": 300,
                "max_retries": 0,
                "retry_on_error": False,
            },
            (("arg", AcquireSourceTaskInput),),
        ),
        (
            "app.queue.tasks.completion",
            "scrape_html_body",
            "scrape_html_body",
            {
                "queue_name": "pipeline:completion",
                "timeout": 60,
                "max_retries": 0,
                "retry_on_error": False,
            },
            (("incomplete_article_id", int),),
        ),
    ],
    ids=["acquisition", "completion"],
)
def test_collection_task_keeps_stage_routing_execution_and_payload_contract(
    task_module: str,
    task_attr: str,
    expected_task_name: str,
    expected_labels: dict[str, object],
    expected_payload: tuple[tuple[str, type[object]], ...],
) -> None:
    """両 task は stage 固有 Stream と既存 task name・payload・実行契約を持つ。"""
    from app.queue.brokers import broker_content

    task = getattr(importlib.import_module(task_module), task_attr)
    signature = inspect.signature(task.original_func)
    hints = get_type_hints(task.original_func)
    payload = tuple(
        (name, hints[name]) for name in signature.parameters if name != "ctx"
    )
    assert (task.broker, task.task_name, task.labels, payload) == (
        broker_content,
        expected_task_name,
        expected_labels,
        expected_payload,
    )


@pytest.mark.parametrize(
    ("task_module", "task_attr", "expected_labels"),
    [
        (
            "app.queue.tasks.acquisition",
            "dispatch_high",
            {
                "timeout": 60,
                "max_retries": 1,
                "retry_on_error": True,
                "schedule": [{"cron": "*/15 * * * *"}],
            },
        ),
        (
            "app.queue.tasks.acquisition",
            "dispatch_medium",
            {
                "timeout": 60,
                "max_retries": 1,
                "retry_on_error": True,
                "schedule": [{"cron": "0 * * * *"}],
            },
        ),
        (
            "app.queue.tasks.acquisition",
            "dispatch_low",
            {
                "timeout": 60,
                "max_retries": 1,
                "retry_on_error": True,
                "schedule": [{"cron": "0 */6 * * *"}],
            },
        ),
        (
            "app.queue.tasks.acquisition",
            "dispatch_sources",
            {"timeout": 60, "max_retries": 1, "retry_on_error": True},
        ),
        (
            "app.queue.tasks.completion",
            "dispatch_html_fetch_jobs",
            {
                "timeout": 30,
                "max_retries": 1,
                "retry_on_error": True,
                "schedule": [{"cron": "* * * * *"}],
            },
        ),
        (
            "app.queue.tasks.completion",
            "sweep_expired_leases",
            {
                "timeout": 30,
                "max_retries": 1,
                "retry_on_error": True,
                "schedule": [{"cron": "* * * * *"}],
            },
        ),
    ],
    ids=[
        "dispatch-high",
        "dispatch-medium",
        "dispatch-low",
        "dispatch-sources",
        "dispatch-completion",
        "sweep-completion",
    ],
)
def test_collection_control_task_keeps_metadata_routing_and_execution_contract(
    task_module: str,
    task_attr: str,
    expected_labels: dict[str, object],
) -> None:
    """dispatch / sweep は metadata broker と既存 task name・labels を維持する。"""
    from app.queue.brokers import broker_metadata

    task = getattr(importlib.import_module(task_module), task_attr)
    assert (task.broker, task.task_name, task.labels) == (
        broker_metadata,
        task_attr,
        expected_labels,
    )


@pytest.mark.parametrize(
    ("task_module", "task_attr", "expected_task_name", "expected_labels"),
    [
        (
            "app.queue.tasks.curation",
            "curate_content",
            "curate_content",
            {
                "queue_name": "pipeline:curation",
                "timeout": 180,
                "max_retries": 1,
                "retry_on_error": True,
            },
        ),
        (
            "app.queue.tasks.assessment",
            "assess_content",
            "assess_content",
            {
                "queue_name": "pipeline:assessment",
                "timeout": 180,
                "max_retries": 2,
                "retry_on_error": True,
            },
        ),
    ],
    ids=["curation", "assessment"],
)
def test_analysis_task_keeps_stage_routing_and_execution_labels(
    task_module: str,
    task_attr: str,
    expected_task_name: str,
    expected_labels: dict[str, object],
) -> None:
    """両 task は共有 broker のまま stage 固有 Stream と既存実行契約を持つ。"""
    import importlib

    from app.queue.brokers import broker_analysis

    task = getattr(importlib.import_module(task_module), task_attr)
    assert (task.broker, task.task_name, task.labels) == (
        broker_analysis,
        expected_task_name,
        expected_labels,
    )


def test_analysis_worker_keeps_single_shared_runtime() -> None:
    """curation / assessment は単一 process と既存 ACK・並列度を共有する。"""
    assert _analysis_worker_commands() == [
        [
            "taskiq",
            "worker",
            "--workers",
            "1",
            "--max-async-tasks",
            "10",
            "app.queue.brokers:broker_analysis",
            "app.queue.tasks.curation",
            "app.queue.tasks.assessment",
            "--ack-type",
            "when_executed",
        ]
    ]


def test_collection_workers_keep_two_program_shared_runtime() -> None:
    """collection は metadata / content の2 processと既存並列度を維持する。"""
    assert _fetch_worker_commands() == {
        "metadata": [
            "taskiq",
            "worker",
            "--workers",
            "1",
            "--max-async-tasks",
            "10",
            "app.queue.brokers:broker_metadata",
            "app.queue.tasks.acquisition",
            "app.queue.tasks.completion",
            "--ack-type",
            "when_executed",
        ],
        "content": [
            "taskiq",
            "worker",
            "--workers",
            "1",
            "--max-async-tasks",
            "5",
            "app.queue.brokers:broker_content",
            "app.queue.tasks.acquisition",
            "app.queue.tasks.completion",
            "--ack-type",
            "when_executed",
        ],
    }


def test_collection_lifecycle_keeps_pool_and_scheduler_boundary() -> None:
    """metadata/content poolは各5/5で、collection schedulerはmetadataだけを使う。"""
    from app.queue.brokers import broker_content, broker_metadata
    from app.queue.schedulers import (
        scheduler_agent,
        scheduler_briefing,
        scheduler_maintenance,
        scheduler_metadata,
        scheduler_trend_discovery,
    )

    scheduler_brokers = tuple(
        scheduler.broker
        for scheduler in (
            scheduler_metadata,
            scheduler_trend_discovery,
            scheduler_agent,
            scheduler_briefing,
            scheduler_maintenance,
        )
    )
    assert (
        WORKER_POOL_SIZING["metadata"],
        WORKER_POOL_SIZING["content"],
        scheduler_metadata.broker,
        broker_content in scheduler_brokers,
    ) == ((5, 5), (5, 5), broker_metadata, False)


def test_maintenance_worker_imports_queue_health_without_runtime_split() -> None:
    """queue samplerは既存maintenance workerのmoduleとして同じruntimeを使う。"""
    assert _maintenance_worker_commands() == [
        [
            "taskiq",
            "worker",
            "--workers",
            "1",
            "--max-async-tasks",
            "10",
            "app.queue.brokers:broker_maintenance",
            "app.queue.tasks.backfill",
            "app.queue.tasks.retention",
            "app.queue.tasks.queue_health",
            "--ack-type",
            "when_executed",
        ]
    ]


class TestCadenceCronMapping:
    """``CADENCE_CRON`` が全 tier を 5-field cron に写像する。"""

    def test_every_cadence_tier_has_a_cron(self) -> None:
        """tier → cron 写像が全 ``FetchCadence`` メンバを網羅する (全域性)。"""
        assert set(CADENCE_CRON) == set(FetchCadence)

    def test_each_cron_has_five_fields(self) -> None:
        """各 cron 式が 5 フィールド (taskiq cron 形式) であること。"""
        for cadence, cron in CADENCE_CRON.items():
            assert len(cron.split()) == 5, f"{cadence} cron must be 5-field: {cron!r}"


@pytest.mark.asyncio
async def test_wire_analysis_adapters_attaches_adapters_to_state() -> None:
    """broker_analysis の WORKER_STARTUP で adapter が state に attach される。

    Provider 選択を hardcode する設計 (Pure DI) を構造的に保証する。
    """
    from app.analysis.assessment.ai.deepseek import DeepSeekAssessor
    from app.analysis.curation.ai.gemini import GeminiCurator
    from app.queue.composition import _wire_analysis_adapters

    state = TaskiqState()
    with (
        patch("app.analysis.curation.ai.gemini.settings") as mock_es,
        patch("app.analysis.assessment.ai.deepseek.settings") as mock_cs,
    ):
        mock_es.gemini_api_key = SecretStr("test-key")
        mock_cs.deepseek_api_key = SecretStr("test-key")
        await _wire_analysis_adapters(state)

    assert isinstance(state.curator, GeminiCurator)
    assert isinstance(state.assessor, DeepSeekAssessor)


@pytest.mark.asyncio
async def test_wire_briefing_adapter_attaches_generator_to_state() -> None:
    """broker_briefing 起動時に briefing generator が state へ attach される。

    briefing の AI provider 選択も composition root で hardcode する設計 (Pure DI) を
    構造的に保証する (analysis / embedding と同じ集約点)。
    """
    from app.insights.briefing.llm import DeepSeekBriefingGenerator
    from app.queue.composition import _wire_briefing_adapter

    state = TaskiqState()
    with patch("app.insights.briefing.llm.settings") as mock_settings:
        mock_settings.deepseek_api_key = SecretStr("test-key")
        await _wire_briefing_adapter(state)

    assert isinstance(state.briefing_generator, DeepSeekBriefingGenerator)


class TestWorkerMaxAsyncTasksCeiling:
    """全 worker が ``--max-async-tasks`` を明示し、各値が pool cap 以下に収まる。

    通常パスの上限ガード。狙いは taskiq 既定 (100) への暗黙依存を断ち、起動時 backlog の
    thundering herd で pool が即枯渇するのを防ぐこと。error-path で別 audit session を
    開く経路 (acquisition の変換棄却 / curation の ready-build 失敗) があり、これは
    飽和不可能の証明ではない。1 task が瞬間的に 2 connection を握りうる分は
    ``max_overflow`` + ``pool_timeout`` fail-fast で吸収する前提。
    """

    def test_every_worker_declares_max_async_tasks(self) -> None:
        # taskiq 既定 100 への暗黙依存を禁止: 全 worker が並列度を明示する
        missing = [label for label, m in _parse_worker_programs().items() if m is None]
        assert not missing, f"workers without explicit --max-async-tasks: {missing}"

    def test_max_async_tasks_within_pool_cap(self) -> None:
        # 各 worker の同時実行が pool cap (pool_size + max_overflow) を超えない。
        # 境界: content=5<=10, trend_discovery=2<=4 (cap を下げると当該 worker が落ちる)
        for label, max_async in _parse_worker_programs().items():
            pool_size, max_overflow = WORKER_POOL_SIZING[label]
            cap = pool_size + max_overflow
            assert max_async is not None and max_async <= cap, (
                f"{label}: --max-async-tasks {max_async} exceeds pool cap {cap}"
            )


class TestWorkerPoolSizing:
    """worker engine が ``WORKER_POOL_SIZING`` どおりに作られ deploy 集合と一致する。"""

    def test_sizing_keys_match_deployed_workers(self) -> None:
        # WORKER_POOL_SIZING が supervisord の deploy worker 集合と一致する
        # (新 worker の sizing 追加漏れ / stale entry を構造的に検出する)
        assert set(_parse_worker_programs()) == set(WORKER_POOL_SIZING)

    def test_common_worker_pool_sizing(self) -> None:
        # 共通 worker は pool_size=5 / max_overflow=5 (cap 10) の均一小型
        pool = build_worker_engine("content").sync_engine.pool
        assert (pool.size(), pool._max_overflow) == (5, 5)

    def test_trend_discovery_pool_sizing(self) -> None:
        # trend_discovery のみ 2/2 に縮小 (日次・fan-out なし・最大 1 connection)
        pool = build_worker_engine("trend_discovery").sync_engine.pool
        assert (pool.size(), pool._max_overflow) == (2, 2)

    def test_worker_recycle_overrides_factory_default(self) -> None:
        # worker は recycle=240 で factory 既定 (3600) を override (autosuspend 手前)
        pool = build_worker_engine("content").sync_engine.pool
        assert pool._recycle == WORKER_POOL_RECYCLE_SECONDS == 240


class TestWorkerApplicationName:
    """worker engine の application_name を検証する。"""

    def test_application_name_matches_service_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _spy(clean_url: str, **kw: Any) -> Any:
            captured.update(kw)
            return _real_create_async_engine(clean_url, **kw)

        monkeypatch.setattr(db_ssl, "create_async_engine", _spy)
        build_worker_engine("content")
        server_settings = captured["connect_args"]["server_settings"]
        assert server_settings["application_name"] == worker_service_name("content")


@pytest.mark.asyncio
async def test_maintenance_startup_logs_auth_engine_failure_without_raising() -> None:
    """auth retention engine 初期化失敗は maintenance worker startup を落とさない。"""
    from app.queue.brokers import broker_maintenance

    handler = broker_maintenance.event_handlers[TaskiqEvents.WORKER_STARTUP][0]
    state = TaskiqState()

    with (
        patch("app.queue.lifecycle.setup_logfire"),
        patch("app.queue.lifecycle.build_worker_engine", return_value=MagicMock()),
        patch(
            "app.queue.lifecycle.build_auth_retention_engine",
            side_effect=ValueError("bad AUTH_RETENTION_DATABASE_URL"),
        ),
        patch("app.queue.lifecycle.logfire.instrument_sqlalchemy"),
        patch("app.queue.lifecycle.log_pool_initialized"),
        patch("app.queue.lifecycle.register_pool_metrics"),
        capture_logs() as logs,
    ):
        await handler(state)

    assert hasattr(state, "session_factory")
    assert not hasattr(state, "auth_session_factory")
    assert any(
        log["event"] == "maintenance_auth_retention_engine_failed"
        and log["error_type"] == "ValueError"
        for log in logs
    )
    assert any(log["event"] == "maintenance_worker_startup" for log in logs)
