"""Queue 分割に伴う operator artifact の静的契約テスト。"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = _REPOSITORY_ROOT / "Makefile"
_COMPOSE_FILE = _REPOSITORY_ROOT / "docker-compose.yml"


def _required_text(path: Path) -> str:
    assert path.is_file(), f"required operator artifact is missing: {path}"
    return path.read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _contains_any(text: str, choices: tuple[str, ...]) -> bool:
    return any(choice in text for choice in choices)


def _compose_comment_text() -> str:
    return "\n".join(
        line.lstrip()[1:].strip()
        for line in _required_text(_COMPOSE_FILE).splitlines()
        if line.lstrip().startswith("#")
    )


def test_makefile_does_not_keep_dead_queues_variable() -> None:
    makefile = _required_text(_MAKEFILE)

    assert re.search(r"(?m)^\s*QUEUES\s*(?::=|\?=|\+=|=)", makefile) is None


def test_compose_comment_calls_maxlen_retained_history_not_backlog() -> None:
    comments = _normalized(_compose_comment_text())

    assert (
        "maxlen" in comments
        and _contains_any(comments, ("retained", "保持履歴", "保持 entry", "保持件数"))
        and _contains_any(
            comments,
            (
                "backlog ではなく",
                "backlogではなく",
                "not backlog",
                "queue depth ではなく",
                "キュー深度ではなく",
            ),
        )
    )
