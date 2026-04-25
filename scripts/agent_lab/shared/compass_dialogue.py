"""Dialogue directory layout for Compass sessions."""

from __future__ import annotations

from pathlib import Path

from shared.dialogue import make_slug


class CompassDialogue:
    """1 セッション分のファイル配置を司る。

    layout:
        {base}/{slug}/
            topic.md                              # 入力題材
            contributions/{specialist}.md         # Round 1 outputs
            DISCUSSION.md                         # Round 2 output (synthesizer)
    """

    def __init__(self, base: Path, slug: str) -> None:
        self.root = base / slug
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "contributions").mkdir(exist_ok=True)

    @property
    def topic_path(self) -> Path:
        return self.root / "topic.md"

    @property
    def contributions_dir(self) -> Path:
        return self.root / "contributions"

    def contribution_path(self, specialist_name: str) -> Path:
        return self.contributions_dir / f"{specialist_name}.md"

    @property
    def final_path(self) -> Path:
        return self.root / "DISCUSSION.md"

    def write_topic(self, content: str) -> None:
        self.topic_path.write_text(content, encoding="utf-8")


__all__ = ["CompassDialogue", "make_slug"]
