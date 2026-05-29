"""Curation — Stage 3 記事キュレーション (選別 + 翻訳要約 + signal/noise 振り分け)。

``ai/`` 配下 (``BaseCurator`` / ``GeminiCurator`` / ``GeminiCurationPrompt`` /
``CurationCall``) は BC 内部実装として閉じる。AI provider 抽象を取りたい場合は
``app.analysis.curation.ai.base`` 等の深い path から取得すること。
``embedding`` / ``assessment`` パッケージとの対称性 (ai/ 配下を ``__init__.py`` から
re-export しない) を維持する。
"""

from app.analysis.curation.domain import (
    CurationResult,
    Noise,
    Signal,
)
from app.analysis.curation.domain.ready import (
    CurationReadyBuildBlockedCode,
    CurationReadyBuildBlockedError,
    ReadyForCuration,
)
from app.analysis.curation.repository import CurationRepository
from app.analysis.curation.service import CurationService

__all__ = [
    "CurationRepository",
    "CurationReadyBuildBlockedCode",
    "CurationReadyBuildBlockedError",
    "CurationResult",
    "CurationService",
    "Noise",
    "ReadyForCuration",
    "Signal",
]
