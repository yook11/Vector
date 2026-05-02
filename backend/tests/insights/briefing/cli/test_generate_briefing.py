"""generate_briefing CLI の argparse + dispatch テスト。"""

from __future__ import annotations

from datetime import date

import pytest

from app.insights.briefing.cli.generate_briefing import build_parser


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
