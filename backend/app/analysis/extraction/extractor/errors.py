"""Extraction 経路で **記事 DELETE 対象** となる内容起因 Permanent failure 例外。

PR3-a-1 で新設した 2 個のみを定義する。両者とも **記事を残しても再試行で
回復しない**:

- ``ExtractionPolicyBlockedError``: Gemini が SAFETY/RECITATION/BLOCKLIST/
  PROHIBITED_CONTENT/SPII を理由に応答を返さなかった。原文そのものが
  Gemini 側のポリシーに抵触するため再試行 / 別モデルでも通らない。
- ``ExtractionInputTooLargeError``: context window 超過。本文サイズが
  Gemini の上限を超えており再試行で回復しない (より大きなモデルでも
  非現実的にコストがかかる)。

両者は ``tasks.py`` で catch され、``ExtractionService.mark_article_unprocessable``
が同 tx で audit INSERT → article DELETE を実行する (`docs/observability/
pipeline-events-design.md` の DELETE 規律)。

親クラスは ``Exception`` 直下とする。既存 ``AnalysisDomainError`` 階層は
全 subtype が infrastructure-bound (ConfigurationError / ProviderError /
NetworkError 等) であり、本 2 例外の意味 (内容起因 Permanent) と階層的に
噛み合わない。階層自体の整理は PR3.5 で ``AIProviderError`` リネームと
合わせて行う予定 (本 PR では既存階層を触らない)。
"""

from __future__ import annotations


class ExtractionPolicyBlockedError(Exception):
    """Gemini が finish_reason=SAFETY/RECITATION/BLOCKLIST/PROHIBITED_CONTENT/SPII。

    記事 DELETE 対象。``raw_response`` は (もしあれば) 監査 payload に焼付ける
    ため 2KB 上限で保持する。
    """

    _RAW_RESPONSE_LIMIT = 2048

    def __init__(
        self,
        *,
        finish_reason: str,
        raw_response: str | None = None,
        prompt_version: str,
    ) -> None:
        self.finish_reason = finish_reason
        truncated = (raw_response or "")[: self._RAW_RESPONSE_LIMIT]
        self.raw_response: str | None = truncated or None
        self.prompt_version = prompt_version
        super().__init__(f"blocked by policy: {finish_reason}")


class ExtractionInputTooLargeError(Exception):
    """context window 超過。記事 DELETE 対象。

    ``_translate_error`` の "exceeds context length" / "context_length_exceeded"
    パターン検出経路から raise される。それ以外の ``InvalidInputError`` 経路
    (空入力 / schema 不正等) は ``ExtractionService`` 内で
    ``InvalidInputOutcome`` に降格し、本例外には進まない。
    """

    def __init__(self, *, prompt_version: str) -> None:
        self.prompt_version = prompt_version
        super().__init__("input exceeds context length")
