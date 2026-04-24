"""Dialogue directory layout for Forge sessions."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


def make_slug(explicit: str | None) -> str:
    """題材から dialogue ディレクトリ名を生成。未指定ならタイムスタンプのみ。"""
    if explicit:
        cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", explicit).strip("-").lower()
        if cleaned:
            return f"{datetime.now().strftime('%Y%m%d-%H%M')}_{cleaned}"
    return datetime.now().strftime("%Y%m%d-%H%M%S")


class Dialogue:
    """1 セッション分のファイル配置を司る。

    layout:
        {base}/{slug}/
            topic.md                              # 入力題材
            v1.md                                 # Round 1 output (planner)
            contributions/{specialist}.md         # Round 2 outputs
            PLAN.md                               # Round 3 output (synthesizer)
    """

    def __init__(self, base: Path, slug: str) -> None:
        self.root = base / slug
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "contributions").mkdir(exist_ok=True)

    @property
    def topic_path(self) -> Path:
        return self.root / "topic.md"

    @property
    def v1_path(self) -> Path:
        return self.root / "v1.md"

    @property
    def contributions_dir(self) -> Path:
        return self.root / "contributions"

    def contribution_path(self, specialist_name: str) -> Path:
        return self.contributions_dir / f"{specialist_name}.md"

    @property
    def final_path(self) -> Path:
        return self.root / "PLAN.md"

    def write_topic(self, content: str) -> None:
        self.topic_path.write_text(content, encoding="utf-8")
