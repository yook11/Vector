"""generate_snapshot CLI のテスト。

検証する観点:
- ``build_parser``: --window-end / --force の解釈、不正フォーマットの拒否
- ``run``: Ready 構築 + Service.execute(ready) 経路で exit code = 0
- ``run`` の dispatch:
  - --window-end 指定なし → ``latest_window_end(now_in_jst())`` で算出
  - --window-end 指定あり → そのまま使用 (任意の JST 日付を許容)
  - --force=True で既存を上書き
  - 既存あり + force=False → "skipped existing:" 出力 (Ready が None)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.insights.snapshot.application.snapshot import Generated
from app.insights.snapshot.cli.generate_snapshot import build_parser, run
from app.insights.snapshot.domain.ready import ReadyForDigest

JST = ZoneInfo("Asia/Tokyo")


# ---------------------------------------------------------------------------
# Fake service / session_factory
# ---------------------------------------------------------------------------


@dataclass
class _FakeCalls:
    executed: list[ReadyForDigest] = field(default_factory=list)


class _FakeService:
    def __init__(self, *, outcome: Generated | None = None) -> None:
        self.calls = _FakeCalls()
        self._outcome = outcome

    async def execute(self, ready: ReadyForDigest) -> Generated:
        self.calls.executed.append(ready)
        if self._outcome is None:
            raise AssertionError("execute not expected for this test")
        return self._outcome


def _fake_session_factory() -> MagicMock:
    """``async with session_factory()`` を fake する callable を返す。

    実 DB に触らない (Ready 構築段階の SnapshotRepository は patch される前提)。
    """
    session = MagicMock()
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=session_ctx)


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_default_no_window_end_no_force(self) -> None:
        args = build_parser().parse_args([])
        assert args.window_end is None
        assert args.force is False

    def test_parses_window_end_iso(self) -> None:
        args = build_parser().parse_args(["--window-end=2026-05-03"])
        assert args.window_end == date(2026, 5, 3)

    def test_force_flag(self) -> None:
        args = build_parser().parse_args(["--force"])
        assert args.force is True

    def test_combines_window_end_and_force(self) -> None:
        args = build_parser().parse_args(["--window-end=2026-05-03", "--force"])
        assert args.window_end == date(2026, 5, 3)
        assert args.force is True

    def test_invalid_window_end_format_exits(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["--window-end=not-a-date"])
        assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# run() — dispatch & exit code
# ---------------------------------------------------------------------------


class TestRun:
    @pytest.mark.asyncio
    async def test_no_window_end_uses_latest_window_end(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--window-end 省略時: ``latest_window_end(now_in_jst())`` で算出する。"""
        ready = ReadyForDigest(window_end=date(2026, 5, 3), force=False)
        service = _FakeService(
            outcome=Generated(window_end=date(2026, 5, 3), source_analysis_count=42)
        )
        args = build_parser().parse_args([])

        with (
            patch(
                "app.insights.snapshot.cli.generate_snapshot.now_in_jst",
                return_value=datetime(2026, 5, 3, 0, 5, tzinfo=JST),
            ),
            patch.object(
                ReadyForDigest, "try_advance_from", new=AsyncMock(return_value=ready)
            ) as advance,
        ):
            rc = await run(args, service, _fake_session_factory())  # type: ignore[arg-type]

        assert rc == 0
        # latest_window_end(2026-05-03 JST 00:05) = 2026-05-03
        assert advance.await_args is not None
        assert advance.await_args.kwargs["window_end"] == date(2026, 5, 3)
        assert advance.await_args.kwargs["force"] is False
        assert service.calls.executed == [ready]
        out = capsys.readouterr().out
        assert "generated" in out
        assert "2026-05-03" in out

    @pytest.mark.asyncio
    async def test_with_window_end_passes_through(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--window-end 指定: その値が ``try_advance_from`` に渡る。"""
        ready = ReadyForDigest(window_end=date(2026, 4, 30), force=False)
        service = _FakeService(
            outcome=Generated(window_end=date(2026, 4, 30), source_analysis_count=12)
        )
        args = build_parser().parse_args(["--window-end=2026-04-30"])

        with patch.object(
            ReadyForDigest, "try_advance_from", new=AsyncMock(return_value=ready)
        ) as advance:
            rc = await run(args, service, _fake_session_factory())  # type: ignore[arg-type]

        assert rc == 0
        assert advance.await_args is not None
        # 2026-04-30 は木曜 — 任意の曜日を受け入れる
        assert advance.await_args.kwargs["window_end"] == date(2026, 4, 30)
        assert service.calls.executed == [ready]

    @pytest.mark.asyncio
    async def test_force_propagates_to_try_advance_from(self) -> None:
        """--force: ``force=True`` が ``try_advance_from`` に渡る。"""
        ready = ReadyForDigest(window_end=date(2026, 5, 3), force=True)
        service = _FakeService(
            outcome=Generated(window_end=date(2026, 5, 3), source_analysis_count=1)
        )
        args = build_parser().parse_args(["--window-end=2026-05-03", "--force"])

        with patch.object(
            ReadyForDigest, "try_advance_from", new=AsyncMock(return_value=ready)
        ) as advance:
            rc = await run(args, service, _fake_session_factory())  # type: ignore[arg-type]

        assert rc == 0
        assert advance.await_args is not None
        assert advance.await_args.kwargs["force"] is True

    @pytest.mark.asyncio
    async def test_skipped_existing_when_ready_is_none(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """try_advance_from が None: Service を呼ばず "skipped existing" を出力。"""
        service = _FakeService(outcome=None)
        args = build_parser().parse_args(["--window-end=2026-05-03"])

        with patch.object(
            ReadyForDigest, "try_advance_from", new=AsyncMock(return_value=None)
        ):
            rc = await run(args, service, _fake_session_factory())  # type: ignore[arg-type]

        assert rc == 0
        assert service.calls.executed == []
        out = capsys.readouterr().out
        assert "skipped existing" in out
        assert "use --force" in out
        assert "2026-05-03" in out
