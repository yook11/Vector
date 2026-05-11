"""Stage 4 ACL: AI 応答 dict → ``AssessmentResult`` の parse 関数。

Gemini / DeepSeek の SDK text response を ``json.loads`` した dict を受け取り、
ドメイン型 (``InScope`` | ``OutOfScope``) に詰め替える。本関数が AI 出力の
ドメイン境界を 1 箇所に集約する (``category == OUT_OF_SCOPE`` 分岐含む)。

provider 非依存 — Gemini / DeepSeek の両 assessor から共通で呼ばれる前提で
provider 固有の SDK 例外翻訳は各 assessor 実装側 (``gemini.py`` / ``deepseek.py``)
に分離する。

設計詳細: ``specs/pipeline-events-stage4-assessment.md`` §Assessor 公開型
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.analysis.assessment.ai.schema import (
    AssessmentResult,
    InScope,
    InScopeCategory,
    OutOfScope,
    ValidCategory,
)
from app.analysis.assessment.errors import AssessmentResponseInvalidError
from app.analysis.domain.value_objects.topic import TopicName


def parse_assessment(payload: dict[str, Any]) -> AssessmentResult:
    """AI が返した flat dict を ``AssessmentResult`` に詰める。

    ``category == OUT_OF_SCOPE`` で ``OutOfScope`` に振り分け、それ以外は
    ``InScope`` を構築。AI 出力のドメイン境界を 1 箇所に集約する。

    Args:
        payload: AI SDK text response を ``json.loads`` した dict。
            必須 key: ``category`` / ``topic`` / ``investor_take``。
            ``OutOfScope`` 経路でも 3 key すべて存在 + ``str`` 型である必要がある
            (AI には常に flat schema を要求しているため、key 欠落は AI 側の
            schema 違反 = 境界で可視化すべき故障)。

    Raises:
        AssessmentResponseInvalidError: schema 違反 (key 欠落 / 型不一致 /
            ``ValidCategory`` enum 外値 / Pydantic ``ValidationError``)。

    Strict 化方針 (PR2 で確定):
        AI 応答 dict の 3 値 (``category`` / ``topic`` / ``investor_take``) すべてを
        ``isinstance(..., str)`` で先頭で厳密に検証する。``str(...)`` 暗黙 coerce は
        使わない (``str(None) == "None"`` / ``str(123) == "123"`` のような silent
        通過を許さない)。``OutOfScope`` 経路でも ``topic`` key の存在と ``str`` 型は
        検証するが、``TopicName`` VO の正規化制約 (3 語、stopword 排除等) は
        ``OutOfScope`` には適用しない (``topic=""`` のような空文字列は raw str として
        通す)。
    """
    try:
        category_raw = payload["category"]
        topic_raw = payload["topic"]
        investor_take_raw = payload["investor_take"]
        if not isinstance(category_raw, str):
            raise ValueError(
                f"'category' must be str, got {type(category_raw).__name__}"
            )
        if not isinstance(topic_raw, str):
            raise ValueError(f"'topic' must be str, got {type(topic_raw).__name__}")
        if not isinstance(investor_take_raw, str):
            raise ValueError(
                f"'investor_take' must be str, got {type(investor_take_raw).__name__}"
            )

        category = ValidCategory(category_raw)
        if category == ValidCategory.OUT_OF_SCOPE:
            return OutOfScope(investor_take=investor_take_raw)
        return InScope(
            category=InScopeCategory(category.value),
            topic=TopicName(topic_raw),  # VO 正規化は InScope のみに適用
            investor_take=investor_take_raw,
        )
    except (KeyError, ValueError, ValidationError) as exc:
        raise AssessmentResponseInvalidError(
            f"AI response schema mismatch: {exc}"
        ) from exc
