"""cl-nagoya/ruri-v3-310m を用いた Ruri Embedder 実装。

TEI (Text Embeddings Inference) サーバーへの HTTP リクエストでベクトルを取得する。

Note:
    本モジュールは docker compose 内 ``embedding:80`` への内部固定通信のみを
    行うため、SSRF 検証付きの ``make_safe_async_client`` ではなく
    ``httpx.AsyncClient`` を直接使う (内部 IP に解決される TEI へ届かなくなる)。
    Ruff の ``TID251`` 禁止は ``pyproject.toml`` の per-file-ignore で除外済み。
    将来 internal client が複数になったら専用ファクトリへの昇格を検討する。
"""

from __future__ import annotations

import httpx

from app.analysis.embedder.base import BaseEmbedder
from app.analysis.errors import (
    AnalysisDomainError,
    InvalidInputError,
    NetworkError,
    ProviderError,
    UnclassifiedError,
)


class RuriEmbedder(BaseEmbedder):
    """BaseEmbedder の ruri-v3-310m 実装。"""

    MODEL = "cl-nagoya/ruri-v3-310m"
    DIMENSION = 768
    RPM = None
    RPD = None
    DOCUMENT_PREFIX = "検索文書: "
    QUERY_PREFIX = "検索クエリ: "

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def _call_api(self, contents: str | list[str]) -> list[list[float]]:
        """TEI の /embed エンドポイントにリクエストする。"""
        inputs = [contents] if isinstance(contents, str) else contents
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._base_url}/embed",
                json={"inputs": inputs},
                timeout=30.0,
            )
            response.raise_for_status()
            return response.json()

    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """HTTP/ネットワーク例外をエラー階層に分類する。"""
        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
            return NetworkError(f"{type(exc).__name__}: {exc}")

        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if 400 <= status < 500:
                return InvalidInputError(f"HTTP {status}: {exc.response.text}")
            if status >= 500:
                return ProviderError(f"HTTP {status}: {exc.response.text}")

        return UnclassifiedError(f"{type(exc).__name__}: {exc}")
