"""Layer 2-A: AI provider 呼び出し由来のエラー 9 種。

``app/analysis/`` 直下に置き、各 Stage の ``errors.py`` (Stage 3/4/5) から import
される横断 module。Stage 横断で共有される provider 由来の例外語彙を 1 ファイルに
集約する。

extractor / assessor / embedder の client (Gemini / DeepSeek) が provider 例外を
ここに翻訳する。各 ``AIProvider*Error`` 自体は **Stage 中立な語彙のみ** を表現し、
Stage 固有の dispatch 軸 (article DELETE / Keep / Retryable など) は持たない。
Stage 境界の ACL (``map_provider_to_extraction`` / ``map_provider_to_assessment``
/ ``to_embedding_error``) が tuple で分類して各 Stage の Layer 1 marker に詰め
替える。

- ``CODE``: ``pipeline_events.code`` カラムへ直接書き込む文字列 (型 SSoT)。
  ACL は本 ``CODE`` を Stage marker の ``code`` instance attr に引き継ぐ。

詳細: ``specs/pipeline-events-error-taxonomy.md`` §Layer 2-A

Phase 4: 基底を ``VectorDomainError`` に変更し、``SAFE_ATTRS=("CODE",)`` で
``__str__`` を class name + CODE のみに固定する (Logfire SaaS への PII 流出経路
封鎖)。constructor は legacy 互換のため ``*args/**kwargs`` を受け取るが
**捨てる** (translator / client が SDK 生 message を渡しても ``__str__`` には
出ない)。
"""

from __future__ import annotations

from typing import Any, ClassVar

from app.logfire_exceptions import VectorDomainError


class AIProviderError(VectorDomainError):
    """AI provider 由来エラーの共通祖先 (Layer 2-A 識別 marker)。

    具体 subclass は ``CODE: ClassVar[str]`` を必ず override する (本クラスは
    抽象 marker、直接 instantiate しない)。型注釈のみ宣言することで Layer 2-A
    全 subclass が ``CODE`` を持つ不変条件を型システム上で表明する。

    Stage 境界 (各 Service の execute) で ACL が本クラスを Stage marker に詰め
    替える。本クラス自体は Stage 固有事情を持たない (foundation marker 多重継承
    は撤去済 = 旧 Stage 3 専用の死荷物だった)。

    Phase 4: ``VectorDomainError`` 継承により ``__str__`` は class name + CODE
    のみ。constructor の ``*args/**kwargs`` は accept-and-discard で SDK 生
    message を捨てる (translator / client の旧 call site 互換を保ちつつ Logfire
    span attribute への PII 漏出を構造的に塞ぐ)。
    """

    CODE: ClassVar[str]
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        # 旧 call site (translator / client) が SDK message を positional で渡す
        # ことがあるが、PII 含有経路のため一切 super に伝えない (Exception の
        # default args を空のまま)。
        super().__init__()


# ---------------------------------------------------------------------------
# provider が明示的に処理拒否したケース (Stage 3 では DROP_ARTICLE 行き)
# ---------------------------------------------------------------------------


class AIProviderInputRejectedError(AIProviderError):
    """provider が入力を明示的に拒否した。

    policy 違反 / token 超過 / 入力 safety block 等。
    """

    CODE: ClassVar[str] = "ai_error_input_rejected"


class AIProviderOutputBlockedError(AIProviderError):
    """provider が応答を blocked-by-safety / recitation 等で抑制した。"""

    CODE: ClassVar[str] = "ai_error_output_blocked"


# ---------------------------------------------------------------------------
# 運用側修正が必要 (記事は健全、Stage 3 では KEEP_ARTICLE 行き)
# ---------------------------------------------------------------------------


class AIProviderConfigurationError(AIProviderError):
    """API key 不正 / model 名不正 / endpoint misconfig 等。運用者対応で復旧。"""

    CODE: ClassVar[str] = "ai_error_configuration"


class AIProviderRequestInvalidError(AIProviderError):
    """request 構造が provider 仕様に合致しない (caller 側 bug)。"""

    CODE: ClassVar[str] = "ai_error_request_invalid"


class AIProviderInsufficientBalanceError(AIProviderError):
    """残高不足 (DeepSeek HTTP 402 等)。アダプター差し替え or 課金で復旧。"""

    CODE: ClassVar[str] = "ai_error_insufficient_balance"


# ---------------------------------------------------------------------------
# 一時障害 (Stage 3 では RETRYABLE 行き)
# ---------------------------------------------------------------------------


class AIProviderRateLimitedError(AIProviderError):
    """rate limit (HTTP 429 / RESOURCE_EXHAUSTED)。"""

    CODE: ClassVar[str] = "ai_error_rate_limited"


class AIProviderQuotaExhaustedError(AIProviderError):
    """日次 quota (RPD) 到達。翌日まで recover 見込みなしだが再 dispatch 可能。"""

    CODE: ClassVar[str] = "ai_error_quota_exhausted"


class AIProviderServiceUnavailableError(AIProviderError):
    """provider 一時障害 (HTTP 5xx)。"""

    CODE: ClassVar[str] = "ai_error_service_unavailable"


class AIProviderNetworkError(AIProviderError):
    """通信障害 (timeout / connection refused / DNS 失敗等)。"""

    CODE: ClassVar[str] = "ai_error_network"
