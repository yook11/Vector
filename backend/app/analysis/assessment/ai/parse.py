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

from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from app.analysis.assessment.domain.result import (
    AssessmentResult,
    InScope,
    InScopeCategory,
    KeyPoint,
    OutOfScope,
    OutOfScopeCategory,
)
from app.analysis.assessment.errors import AssessmentResponseInvalidError


class AssessmentResponseDefect(StrEnum):
    """parse が検知する「内容の schema 違反」種別 (自己記述コード、provider 非依存)。

    value はそのまま audit の ``outcome_code`` に焼かれる (完成段
    ``AnalyzableArticleDefect`` と同形)。失敗を検知した parse がその語彙を所有し、
    ``AssessmentResponseInvalidError(defect)`` に載せて投げる。各 member は下の
    raise 点と 1:1 対応し、写像漏れが原理的に起きない。

    中身 (AI 生成値 = PII) は焼かず、どの field がどう違反したかの種別ラベルだけを
    残す。provider envelope の違反 (非 JSON / tool_call 欠落等) は各 adapter が別の
    enum で所有する (parse は payload dict を受け取った後の内容違反のみ扱う)。
    """

    # 3 key の欠落 (KeyError)
    CATEGORY_KEY_MISSING = "assessment_response_category_key_missing"
    INVESTOR_TAKE_KEY_MISSING = "assessment_response_investor_take_key_missing"
    KEY_POINTS_KEY_MISSING = "assessment_response_key_points_key_missing"
    # 文字列 / list 型違反 (isinstance 先頭検証)
    CATEGORY_WRONG_TYPE = "assessment_response_category_wrong_type"
    INVESTOR_TAKE_WRONG_TYPE = "assessment_response_investor_take_wrong_type"
    KEY_POINTS_WRONG_TYPE = "assessment_response_key_points_wrong_type"
    # 値違反 (category enum 外値 / key_point 要素検証 / 最終構築の制約違反)
    CATEGORY_UNKNOWN_VALUE = "assessment_response_category_unknown_value"
    KEY_POINT_INVALID = "assessment_response_key_point_invalid"
    INVESTOR_TAKE_INVALID = "assessment_response_investor_take_invalid"
    KEY_POINTS_TOO_MANY = "assessment_response_key_points_too_many"


def _final_construction_defect(
    exc: ValidationError,
) -> AssessmentResponseDefect | None:
    """最終構築 (``InScope`` / ``OutOfScope``) の ``ValidationError`` を分類する。

    ``loc[0]`` が既知 field なら対応 defect を返す。未知 loc は写像漏れを誤ラベル
    しないよう ``None`` を返し、呼び出し側が素の ``ValidationError`` を伝播させる。
    再チェックでなく分類 (Field constraint が捕えた error を field 名で写像する)。
    """
    first_loc = exc.errors()[0]["loc"]
    field = str(first_loc[0]) if first_loc else ""
    if field == "investor_take":
        # 最終構築でのみ起きる: 空 (min_length=1 / _not_empty) または長さ超過。
        return AssessmentResponseDefect.INVESTOR_TAKE_INVALID
    if field == "key_points":
        # 最終構築でのみ起きる: 要素は valid だが件数が max_length=10 を超過。
        return AssessmentResponseDefect.KEY_POINTS_TOO_MANY
    return None


def parse_assessment(payload: dict[str, Any]) -> AssessmentResult:
    """AI が返した flat dict を ``AssessmentResult`` に詰める。

    ``category == OUT_OF_SCOPE`` で ``OutOfScope`` に振り分け、それ以外は
    ``InScope`` を構築。AI 出力のドメイン境界を 1 箇所に集約する。

    Args:
        payload: AI SDK text response を ``json.loads`` した dict。
            必須 key: ``category`` / ``investor_take`` / ``key_points``。
            ``OutOfScope`` 経路でも 3 key すべて存在 + 型一致である必要がある
            (AI には常に flat schema を要求しているため、key 欠落は AI 側の
            schema 違反 = 境界で可視化すべき故障)。

    Raises:
        AssessmentResponseInvalidError: schema 違反 (key 欠落 / 型不一致 /
            category enum 外値 / Pydantic ``ValidationError``)。

    Strict 化方針:
        AI 応答 dict の 2 文字列値 (``category`` / ``investor_take``) を
        ``isinstance(..., str)`` で、``key_points`` を ``isinstance(..., list)`` で
        先頭検証する。``str(...)`` 暗黙 coerce は使わない (silent 通過を許さない)。
        ``key_points`` は InScope / OutOfScope どちらの経路でも domain に保持する
        (out-of-scope 記事の key_points も検証用途で残す、両 path 対称)。
    """
    # key 取得: 各 key 欠落を個別 defect に分類する (どの key が欠けたか可視化)。
    try:
        category_raw = payload["category"]
    except KeyError as exc:
        raise AssessmentResponseInvalidError(
            AssessmentResponseDefect.CATEGORY_KEY_MISSING
        ) from exc
    try:
        investor_take_raw = payload["investor_take"]
    except KeyError as exc:
        raise AssessmentResponseInvalidError(
            AssessmentResponseDefect.INVESTOR_TAKE_KEY_MISSING
        ) from exc
    try:
        key_points_raw = payload["key_points"]
    except KeyError as exc:
        raise AssessmentResponseInvalidError(
            AssessmentResponseDefect.KEY_POINTS_KEY_MISSING
        ) from exc

    # 型違反: isinstance 先頭検証。自前判定なので原例外 (cause) はない。
    if not isinstance(category_raw, str):
        raise AssessmentResponseInvalidError(
            AssessmentResponseDefect.CATEGORY_WRONG_TYPE
        )
    if not isinstance(investor_take_raw, str):
        raise AssessmentResponseInvalidError(
            AssessmentResponseDefect.INVESTOR_TAKE_WRONG_TYPE
        )
    if not isinstance(key_points_raw, list):
        raise AssessmentResponseInvalidError(
            AssessmentResponseDefect.KEY_POINTS_WRONG_TYPE
        )

    # key_point 要素の Pydantic 検証 (content 空 / mention type 外値 / 非 dict 等)。
    try:
        key_points = [KeyPoint.model_validate(k) for k in key_points_raw]
    except ValidationError as exc:
        # ValidationError は payload 値を含みうるため、公開 message には載せない。
        raise AssessmentResponseInvalidError(
            AssessmentResponseDefect.KEY_POINT_INVALID
        ) from exc

    # 最終構築: InScope / OutOfScope の Field 制約 (investor_take 空/長さ,
    # key_points 件数上限) 違反を field 名で分類する。未知 loc は誤ラベルせず素の
    # ValidationError を伝播させる (task 層が unexpected_error に surface する)。
    try:
        if category_raw == OutOfScopeCategory.OUT_OF_SCOPE.value:
            return OutOfScope(
                investor_take=investor_take_raw,
                key_points=key_points,
            )
        try:
            in_scope_category = InScopeCategory(category_raw)
        except ValueError as exc:
            raise AssessmentResponseInvalidError(
                AssessmentResponseDefect.CATEGORY_UNKNOWN_VALUE
            ) from exc
        return InScope(
            category=in_scope_category,
            investor_take=investor_take_raw,
            key_points=key_points,
        )
    except ValidationError as exc:
        defect = _final_construction_defect(exc)
        if defect is None:
            raise
        raise AssessmentResponseInvalidError(defect) from exc
