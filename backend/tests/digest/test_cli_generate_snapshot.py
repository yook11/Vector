"""generate_snapshot CLI のテスト。

- ``build_parser``: --week / --force の解釈、不正フォーマットの拒否
- ``run``: Generated / Skipped を返す Service を注入して exit code = 0
- ``run`` の dispatch: --week 指定なし → ``generate_for_latest_completed_week``、
  --week 指定あり → ``generate_for_week``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pytest

from app.digest.application.snapshot import Generated, Skipped
from app.digest.cli.generate_snapshot import build_parser, run

# ---------------------------------------------------------------------------
# Fake service (duck-typed; WeeklyTrendsSnapshotService の interface に従う)
# ---------------------------------------------------------------------------


@dataclass
class _FakeCalls:
    latest: list[bool] = field(default_factory=list)  # force flag を記録
    by_week: list[tuple[date, bool]] = field(default_factory=list)


class _FakeService:
    def __init__(
        self,
        *,
        outcome_latest: Generated | Skipped | None = None,
        outcome_by_week: Generated | Skipped | None = None,
    ) -> None:
        self.calls = _FakeCalls()
        self._outcome_latest = outcome_latest
        self._outcome_by_week = outcome_by_week

    async def generate_for_latest_completed_week(
        self, *, force: bool = False
    ) -> Generated | Skipped:
        self.calls.latest.append(force)
        if self._outcome_latest is None:
            raise AssertionError("generate_for_latest_completed_week not expected")
        return self._outcome_latest

    async def generate_for_week(
        self, week_start: date, *, force: bool = False
    ) -> Generated | Skipped:
        self.calls.by_week.append((week_start, force))
        if self._outcome_by_week is None:
            raise AssertionError("generate_for_week not expected")
        return self._outcome_by_week


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_default_no_week_no_force(self) -> None:
        args = build_parser().parse_args([])
        assert args.week is None
        assert args.force is False

    def test_parses_week_iso(self) -> None:
        args = build_parser().parse_args(["--week=2026-04-13"])
        assert args.week == date(2026, 4, 13)

    def test_force_flag(self) -> None:
        args = build_parser().parse_args(["--force"])
        assert args.force is True

    def test_combines_week_and_force(self) -> None:
        args = build_parser().parse_args(["--week=2026-04-13", "--force"])
        assert args.week == date(2026, 4, 13)
        assert args.force is True

    def test_invalid_week_format_exits(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["--week=not-a-date"])
        assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# run() — dispatch & exit code
# ---------------------------------------------------------------------------


class TestRun:
    @pytest.mark.asyncio
    async def test_no_week_calls_latest_completed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        service = _FakeService(
            outcome_latest=Generated(
                week_start=date(2026, 4, 20), source_analysis_count=42
            )
        )
        args = build_parser().parse_args([])
        rc = await run(args, service)  # type: ignore[arg-type]
        assert rc == 0
        assert service.calls.latest == [False]
        assert service.calls.by_week == []
        out = capsys.readouterr().out
        assert "generated" in out
        assert "2026-04-20" in out

    @pytest.mark.asyncio
    async def test_with_week_calls_generate_for_week(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        service = _FakeService(
            outcome_by_week=Generated(
                week_start=date(2026, 4, 13), source_analysis_count=12
            )
        )
        args = build_parser().parse_args(["--week=2026-04-13"])
        rc = await run(args, service)  # type: ignore[arg-type]
        assert rc == 0
        assert service.calls.by_week == [(date(2026, 4, 13), False)]
        assert service.calls.latest == []

    @pytest.mark.asyncio
    async def test_force_propagates(self) -> None:
        service = _FakeService(
            outcome_by_week=Generated(
                week_start=date(2026, 4, 13), source_analysis_count=1
            )
        )
        args = build_parser().parse_args(["--week=2026-04-13", "--force"])
        rc = await run(args, service)  # type: ignore[arg-type]
        assert rc == 0
        assert service.calls.by_week == [(date(2026, 4, 13), True)]

    @pytest.mark.asyncio
    async def test_skipped_returns_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        service = _FakeService(outcome_latest=Skipped(week_start=date(2026, 4, 20)))
        args = build_parser().parse_args([])
        rc = await run(args, service)  # type: ignore[arg-type]
        assert rc == 0
        out = capsys.readouterr().out
        assert "skipped existing" in out
        assert "use --force" in out
