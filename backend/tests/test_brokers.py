"""brokers.py の composition root と worker runtime 設定に関するテスト。"""

import configparser
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from taskiq import TaskiqState

import app.db_ssl as db_ssl
from app.collection.sources.fetch_cadence import FetchCadence
from app.queue.lifecycle import (
    WORKER_POOL_RECYCLE_SECONDS,
    WORKER_POOL_SIZING,
    build_worker_engine,
    worker_service_name,
)
from app.queue.schedule import CADENCE_CRON

# supervisord の worker 定義 (taskiq worker 起動引数の SSoT)。
_SUPERVISORD_DIR = Path(__file__).resolve().parent.parent / "supervisord"


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
