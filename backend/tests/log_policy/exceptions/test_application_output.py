"""境界で生成したアプリ例外の診断を、共通処理とrenderer経由で確認する。"""

import json
from uuid import UUID

import pytest
import structlog

from app.analysis.assessment.ai.parse import parse_assessment
from app.analysis.assessment.errors import AssessmentResponseInvalidError
from app.analysis.assessment.events import (
    ArticleAssessedInScopeEvent,
    AssessedEventInvalidError,
)
from app.log_policy import BASE_LOG_RULES, PolicyLogger, build_processors, policy_logger
from app.log_policy.exceptions.extraction import EXCEPTION_LIMIT

pytestmark = pytest.mark.unit


@pytest.fixture
def validation_failure() -> AssessedEventInvalidError:
    """入力由来の値と未知の項目名を、製品の検証境界で診断へ変換する。"""
    with pytest.raises(AssessedEventInvalidError) as caught:
        ArticleAssessedInScopeEvent.from_input(
            {
                "event_id": str(UUID(int=1)),
                "event_type": "article.assessed_in_scope",
                "schema_version": 1,
                "occurred_at": "2026-09-21T00:00:00Z",
                "payload": {
                    "curation_id": "synthetic-private-value",
                    "analyzed_article_id": 2,
                    "synthetic-private-key": "synthetic-private-input",
                },
            }
        )
    return caught.value


@pytest.fixture
def application_logger(configure_chain):
    """既定の共通変換を通して、実際のJSON出力を返すロガーを作る。"""
    configure_chain()
    structlog.configure(
        processors=build_processors(structlog.processors.JSONRenderer()),
        logger_factory=lambda *_: PolicyLogger(
            BASE_LOG_RULES, structlog.ReturnLogger()
        ),
    )
    return policy_logger("test")


def test_boundary_diagnostics_reach_json_without_input(
    application_logger, validation_failure
) -> None:
    """あらかじめ定義したreasonやissueが記録され、入力は含まれない。"""
    output = json.loads(
        application_logger.error("event_failed", exc_info=validation_failure)
    )

    assert output["error_class"] == (
        "app.analysis.assessment.events.AssessedEventInvalidError"
    )
    assert output["error_message"] == "Validation failed: invalid_payload"
    assert output["error_details"] == {
        "kind": "application_validation",
        "reason": "invalid_payload",
        "issues": [
            {"field": "payload.curation_id", "code": "invalid_type"},
            {"field": "payload", "code": "unknown_field"},
        ],
    }
    assert output["frames"]
    assert "related_exceptions" not in output
    assert "synthetic-private" not in json.dumps(output)


def test_unknown_category_logs_application_diagnostics_without_invalid_value(
    application_logger,
) -> None:
    """カテゴリー不正の説明・分類・発生位置を残し、元の値を原因経由でも出さない。"""
    with pytest.raises(AssessmentResponseInvalidError) as caught:
        parse_assessment(
            {
                "category": "PRIVATE_CATEGORY_SENTINEL",
                "investor_take": "x",
                "key_points": [],
            }
        )

    output = json.loads(
        application_logger.error("assessment_failed", exc_info=caught.value)
    )

    assert output["error_class"] == (
        "app.analysis.assessment.errors.AssessmentResponseInvalidError"
    )
    assert output["error_message"] == "AI応答のcategoryが定義済みの分類ではありません"
    assert output["error_details"] == {
        "reason": "response_invalid",
        "code": "assessment_response_category_unknown_value",
    }
    assert any(frame["function"] == "parse_assessment" for frame in output["frames"])
    assert "PRIVATE_CATEGORY_SENTINEL" not in json.dumps(output)
    assert "related_exceptions" not in output


def test_deepest_exception_keeps_issue_fields_and_codes(
    application_logger, validation_failure
) -> None:
    """件数の上限内で最も内側の例外でも、診断項目のfield・codeまで値準備を通過する。"""
    # 外側を含めた件数の上限の、最後の1件に検証エラーを置く。
    outer: BaseException = validation_failure
    for _ in range(EXCEPTION_LIMIT - 1):
        parent = RuntimeError("operation failed")
        parent.__cause__ = outer
        outer = parent

    output = json.loads(application_logger.error("event_failed", exc_info=outer))

    deepest = output["related_exceptions"][EXCEPTION_LIMIT - 2]["exception"]
    assert deepest["error_details"] == {
        "kind": "application_validation",
        "reason": "invalid_payload",
        "issues": [
            {"field": "payload.curation_id", "code": "invalid_type"},
            {"field": "payload", "code": "unknown_field"},
        ],
    }
