"""Stage 中立な AI provider origin error。

失敗を 2 系統に分ける:

- ``AIProviderStateError``: provider / 環境の状態に起因 (network / 5xx / quota /
  設定不正 等)。回復クラス ``FAILURE_MODE`` を型で固定する。
- ``AIProviderContentError``: 入力 / 出力の内容に起因 (safety block / recitation /
  入力長超過 等)。``FAILURE_MODE`` は ``TARGET_REJECTED`` 固定。

各 error は「回復クラス (mode)」と「詳細 (reason)」を自己記述する。mode は
「起きた後どう対応するか」(待てば治る / 人が直す / 対象を捨てる) の括りで、
handler が retry / hold を導出する。provider の具体的な状態 (5xx / timeout /
safety 等) は mode ではなく ``reason`` (検知箇所が所有する StrEnum) が運ぶ。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from app.logfire.exceptions import VectorDomainError


class AIProviderFailureMode(StrEnum):
    """provider 失敗の「回復クラス」。失敗が起きた後どう回復するか (対応の括り) を表す。

    本 enum が答えるのは次の問い:

    - その実行だけの問題か (ATTEMPT_SCOPED)
    - 待てば回復するか (TIME_BASED_RECOVERY)
    - 回復に条件が要るか (CONDITION_BASED_RECOVERY)
    - 運用者の対応が要るか (OPERATOR_ACTION_REQUIRED)
    - 対象が拒否され回復しないか (TARGET_REJECTED)

    provider の具体的な状態 (5xx / timeout / safety block 等) は本 enum ではなく
    各 error の ``reason`` が運ぶ。handler は本 enum から retry / hold を導出する。
    将来の回復パターン (例: 別の条件付き回復) が要るなら member を足して表す。
    """

    ATTEMPT_SCOPED = "attempt_scoped"
    """その実行だけの問題。別の実行 (即時再試行) で回復しうる (network 一時障害)。"""

    TIME_BASED_RECOVERY = "time_based_recovery"
    """時間経過で回復する。backoff 再試行が有効 (provider 一時不応答 / throttling)。"""

    CONDITION_BASED_RECOVERY = "condition_based_recovery"
    """回復に条件が要る (利用枠の回復待ち)。条件成立まで近い再試行は無効で、
    枯渇時は hold して条件成立を待つ。"""

    OPERATOR_ACTION_REQUIRED = "operator_action_required"
    """運用者の対応なしには回復しない (設定不正 / 要求不正 / 残高不足)。"""

    TARGET_REJECTED = "target_rejected"
    """処理対象が拒否された。回復せず対象を捨てる (content 拒否)。"""

    @property
    def retryable(self) -> bool:
        """再試行で回復しうる回復クラスか。"""
        return self in (
            AIProviderFailureMode.ATTEMPT_SCOPED,
            AIProviderFailureMode.TIME_BASED_RECOVERY,
            AIProviderFailureMode.CONDITION_BASED_RECOVERY,
        )

    @property
    def is_stage_hold_mode(self) -> bool:
        """この回復クラスが stage の退避 (hold) を要するか。

        OPERATOR_ACTION_REQUIRED は運用者対応なしに回復せず、
        CONDITION_BASED_RECOVERY は利用枠の回復を待つ必要があるため stage を
        hold する。hold を「いつ」立てるか (即時か retry 枯渇時か) は consumer
        (handler) が retry の余地と合わせて決める。本 property はどの回復クラスが
        hold を要するかの SSoT。
        """
        return self in (
            AIProviderFailureMode.OPERATOR_ACTION_REQUIRED,
            AIProviderFailureMode.CONDITION_BASED_RECOVERY,
        )


class AIProviderError(VectorDomainError):
    """provider 由来エラーの共通祖先。Stage の処理方針は持たない。

    ``__init__`` は引数を受けて捨てる (accept-and-discard)。SDK 生 message を
    渡しても ``__str__`` (= Logfire span attribute) に乗らない PII 境界を保ち、
    ad-hoc subclass の互換も維持する。回復クラス / reason を持つのは下位 2 系統
    (``AIProviderStateError`` / ``AIProviderContentError``)。
    """

    CODE: ClassVar[str]
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        super().__init__()


class AIProviderStateError(AIProviderError):
    """provider / 環境の状態に起因するエラー。

    ``FAILURE_MODE`` (回復クラス) を型で固定し、leaf に宣言を強制する。「何が
    起きたか」の詳細は ``reason`` (検知箇所所有の StrEnum、timeout / server_error
    / leaked_api_key 等) が任意で運ぶ。reason は forensics 用の instance 属性で、
    ``SAFE_ATTRS`` には含めない (golden な ``str(exc)`` 形を ``(CODE=...)`` に保つ)。
    accept-and-discard は維持し、reason は keyword 専用で追加する。
    """

    FAILURE_MODE: ClassVar[AIProviderFailureMode]
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE",)

    reason: StrEnum | None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "FAILURE_MODE" not in cls.__dict__:
            raise TypeError(f"{cls.__qualname__} must declare FAILURE_MODE")

    def __init__(
        self,
        *args: Any,
        reason: StrEnum | None = None,
        **kwargs: Any,  # noqa: ARG002
    ) -> None:
        if reason is not None and not isinstance(reason, StrEnum):
            # PII 境界: 種別ラベル (StrEnum) 以外を reason に通さない。
            raise TypeError("reason must be a StrEnum member or None")
        super().__init__()
        self.reason = reason


class AIProviderContentError(AIProviderError):
    """入力 / 出力の内容に起因するエラー。

    回復クラスは ``TARGET_REJECTED`` 固定 (再試行無効で対象を捨てる)。「なぜ
    弾かれたか」は ``reason`` (検知箇所所有の StrEnum、safety / recitation /
    context_length 等) が必須で運ぶ。reason は PII-free な種別ラベル (enum value)
    なので ``SAFE_ATTRS`` に含めて forensics に供する。自由文字列 (= AI 生成値)
    を ctor に通さないよう型ガードする。
    """

    FAILURE_MODE: ClassVar[AIProviderFailureMode] = (
        AIProviderFailureMode.TARGET_REJECTED
    )
    SAFE_ATTRS: ClassVar[tuple[str, ...]] = ("CODE", "reason")

    reason: StrEnum

    def __init__(self, *, reason: StrEnum) -> None:
        if not isinstance(reason, StrEnum):
            # PII 境界: 自由文字列 (AI 生成値) を reason に通さない。
            raise TypeError("reason must be a StrEnum member")
        super().__init__()
        self.reason = reason


# ---------------------------------------------------------------------------
# Content 起因 (入出力内容の拒否)。回復クラス = TARGET_REJECTED。
# ---------------------------------------------------------------------------


class AIProviderInputRejectedError(AIProviderContentError):
    """provider が入力を明示的に拒否した。

    policy 違反 / token 超過 / 入力 safety block 等。``reason`` が具体 (input_blocked
    / context_length / safety 等) を運ぶ。
    """

    CODE: ClassVar[str] = "ai_error_input_rejected"


class AIProviderOutputBlockedError(AIProviderContentError):
    """provider が応答を blocked-by-safety / recitation 等で抑制した。

    ``reason`` が finish_reason 由来の具体 (safety / recitation / blocklist /
    prohibited_content / spii) を運ぶ。
    """

    CODE: ClassVar[str] = "ai_error_output_blocked"


# ---------------------------------------------------------------------------
# State 起因: 運用側修正が必要 (記事は健全、Stage 3 では KEEP_ARTICLE 行き)。
# 回復クラス = OPERATOR_ACTION_REQUIRED。
# ---------------------------------------------------------------------------


class AIProviderConfigurationError(AIProviderStateError):
    """API key 不正 / model 名不正 / endpoint misconfig 等。運用者対応で復旧。"""

    CODE: ClassVar[str] = "ai_error_configuration"
    FAILURE_MODE: ClassVar[AIProviderFailureMode] = (
        AIProviderFailureMode.OPERATOR_ACTION_REQUIRED
    )


class AIProviderRequestInvalidError(AIProviderStateError):
    """request 構造が provider 仕様に合致しない。"""

    CODE: ClassVar[str] = "ai_error_request_invalid"
    FAILURE_MODE: ClassVar[AIProviderFailureMode] = (
        AIProviderFailureMode.OPERATOR_ACTION_REQUIRED
    )


class AIProviderInsufficientBalanceError(AIProviderStateError):
    """残高不足 (DeepSeek HTTP 402 等)。アダプター差し替え or 課金で復旧。"""

    CODE: ClassVar[str] = "ai_error_insufficient_balance"
    FAILURE_MODE: ClassVar[AIProviderFailureMode] = (
        AIProviderFailureMode.OPERATOR_ACTION_REQUIRED
    )


# ---------------------------------------------------------------------------
# State 起因: 一時障害 (Stage 3 では RETRYABLE 行き)。
# ---------------------------------------------------------------------------


class AIProviderRateLimitedError(AIProviderStateError):
    """rate limit (HTTP 429 / RESOURCE_EXHAUSTED)。"""

    CODE: ClassVar[str] = "ai_error_rate_limited"
    FAILURE_MODE: ClassVar[AIProviderFailureMode] = (
        AIProviderFailureMode.TIME_BASED_RECOVERY
    )


class AIProviderUsageLimitExhaustedError(AIProviderStateError):
    """provider / account / project / model の利用枠を使い切った。時間経過等で復旧。"""

    CODE: ClassVar[str] = "ai_error_usage_limit_exhausted"
    FAILURE_MODE: ClassVar[AIProviderFailureMode] = (
        AIProviderFailureMode.CONDITION_BASED_RECOVERY
    )


class AIProviderServiceUnavailableError(AIProviderStateError):
    """provider 一時障害 (HTTP 5xx)。"""

    CODE: ClassVar[str] = "ai_error_service_unavailable"
    FAILURE_MODE: ClassVar[AIProviderFailureMode] = (
        AIProviderFailureMode.TIME_BASED_RECOVERY
    )


class AIProviderNetworkError(AIProviderStateError):
    """通信障害 (timeout / connection refused / DNS 失敗等)。"""

    CODE: ClassVar[str] = "ai_error_network"
    FAILURE_MODE: ClassVar[AIProviderFailureMode] = AIProviderFailureMode.ATTEMPT_SCOPED
