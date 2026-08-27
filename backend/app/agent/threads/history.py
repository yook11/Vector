"""Run へ渡す会話履歴の投影。

prompt へ出せる大きさへ揃える責務をここに閉じ、描き方は各工程が持つ。
"""

from __future__ import annotations

from app.agent.threads.contracts import ThreadMessageSnapshot

__all__ = [
    "HISTORY_MESSAGE_CHAR_CAP",
    "HISTORY_MESSAGE_LIMIT",
    "MISSING_ASPECT_CHAR_CAP",
    "MISSING_ASPECT_LIMIT",
    "normalize_run_history",
]

HISTORY_MESSAGE_LIMIT = 6
HISTORY_MESSAGE_CHAR_CAP = 2000
MISSING_ASPECT_CHAR_CAP = 300
MISSING_ASPECT_LIMIT = 8


def normalize_run_history(
    history: list[ThreadMessageSnapshot],
) -> list[ThreadMessageSnapshot]:
    """content を上限へ切り詰め、missing_aspects を履歴全体で重複なく残す。"""
    seen_missing_aspects: set[str] = set()
    normalized: list[ThreadMessageSnapshot] = []
    for message in history:
        missing_aspects: list[str] = []
        if message.role == "assistant":
            for missing_aspect in message.missing_aspects:
                value = _normalize_missing_aspect(missing_aspect)
                if (
                    value
                    and value not in seen_missing_aspects
                    and len(seen_missing_aspects) < MISSING_ASPECT_LIMIT
                ):
                    seen_missing_aspects.add(value)
                    missing_aspects.append(value)
        normalized.append(
            ThreadMessageSnapshot(
                role=message.role,
                content=message.content[:HISTORY_MESSAGE_CHAR_CAP],
                missing_aspects=tuple(missing_aspects),
            )
        )
    return normalized


def _normalize_missing_aspect(value: str) -> str:
    return value.strip()[:MISSING_ASPECT_CHAR_CAP].strip()
