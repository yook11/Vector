"""taskiqへ渡す、元の例外の詳細を落とした失敗。"""

from __future__ import annotations

__all__ = ["AgentRunTaskBoundaryError"]


class AgentRunTaskBoundaryError(RuntimeError):
    """Agent run task boundaryで公開する安全な永続化エラー。"""
