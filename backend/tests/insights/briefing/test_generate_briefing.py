"""generate_briefing CLI の argparse + dispatch テスト。"""

from __future__ import annotations

import argparse
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.insights.briefing.cli import build_parser, run
from app.insights.briefing.domain.ready import ReadyForBriefing


class TestParser:
    def test_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])
        assert args.week is None
        assert args.category is None
        assert args.force is False

    def test_full_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--week=2026-04-20", "--category=ai", "--force"])
        assert args.week == date(2026, 4, 20)
        assert args.category == "ai"
        assert args.force is True

    def test_invalid_week_format_exits(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--week=not-a-date"])


class TestRun:
    @pytest.mark.asyncio
    async def test_existing_briefing_skip_writes_audit(self) -> None:
        args = argparse.Namespace(
            week=date(2026, 4, 20),
            category=None,
            force=False,
        )
        service = MagicMock()
        service.execute = AsyncMock()

        category = MagicMock(id=1, slug="ai")
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[category])
        rows = MagicMock()
        rows.scalars = MagicMock(return_value=scalars)

        session = MagicMock()
        session.execute = AsyncMock(return_value=rows)
        session.commit = AsyncMock()
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session)
        session_ctx.__aexit__ = AsyncMock(return_value=None)
        session_factory = MagicMock(return_value=session_ctx)

        with (
            patch.object(
                ReadyForBriefing,
                "try_advance_from",
                new=AsyncMock(return_value=None),
            ),
            patch("app.insights.briefing.cli.BriefingAuditRepository") as audit_cls,
        ):
            audit_cls.return_value.append_generation_already_exists = AsyncMock()
            exit_code = await run(args, service, session_factory)

        assert exit_code == 0
        service.execute.assert_not_awaited()
        audit_cls.return_value.append_generation_already_exists.assert_awaited_once_with(
            week_start=date(2026, 4, 20),
            category_id=1,
        )
