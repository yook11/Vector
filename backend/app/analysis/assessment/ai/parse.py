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

from app.analysis.assessment.domain.result import (
    AssessmentResult,
    Event,
    InScope,
    InScopeCategory,
    OutOfScope,
    ValidCategory,
)
from app.analysis.assessment.errors import AssessmentResponseInvalidError


def parse_assessment(payload: dict[str, Any]) -> AssessmentResult:
    """AI が返した flat dict を ``AssessmentResult`` に詰める。

    ``category == OUT_OF_SCOPE`` で ``OutOfScope`` に振り分け、それ以外は
    ``InScope`` を構築。AI 出力のドメイン境界を 1 箇所に集約する。

    Args:
        payload: AI SDK text response を ``json.loads`` した dict。
            必須 key: ``category`` / ``investor_take`` / ``events``。
            ``OutOfScope`` 経路でも 3 key すべて存在 + 型一致である必要がある
            (AI には常に flat schema を要求しているため、key 欠落は AI 側の
            schema 違反 = 境界で可視化すべき故障)。

    Raises:
        AssessmentResponseInvalidError: schema 違反 (key 欠落 / 型不一致 /
            ``ValidCategory`` enum 外値 / Pydantic ``ValidationError``)。

    Strict 化方針:
        AI 応答 dict の 2 文字列値 (``category`` / ``investor_take``) を
        ``isinstance(..., str)`` で、``events`` を ``isinstance(..., list)`` で
        先頭検証する。``str(...)`` 暗黙 coerce は使わない (silent 通過を許さない)。
        ``events`` は InScope / OutOfScope どちらの経路でも domain に保持する
        (out-of-scope 記事の events も検証用途で残す、両 path 対称)。
    """
    try:
        category_raw = payload["category"]
        investor_take_raw = payload["investor_take"]
        events_raw = payload["events"]
        if not isinstance(category_raw, str):
            raise ValueError(
                f"'category' must be str, got {type(category_raw).__name__}"
            )
        if not isinstance(investor_take_raw, str):
            raise ValueError(
                f"'investor_take' must be str, got {type(investor_take_raw).__name__}"
            )
        if not isinstance(events_raw, list):
            raise ValueError(f"'events' must be list, got {type(events_raw).__name__}")

        events = [Event.model_validate(e) for e in events_raw]
        category = ValidCategory(category_raw)
        if category == ValidCategory.OUT_OF_SCOPE:
            return OutOfScope(
                investor_take=investor_take_raw,
                events=events,
            )
        return InScope(
            category=InScopeCategory(category.value),
            investor_take=investor_take_raw,
            events=events,
        )
    except (KeyError, ValueError, ValidationError) as exc:
        # Phase 4: 旧 message 引数廃止 (Pydantic ValidationError は payload 値を
        # 含みうる経路)。__cause__ 連鎖は残るので debug 時は traceback で辿れる。
        raise AssessmentResponseInvalidError() from exc
