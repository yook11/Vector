"""境界で生成したアプリ例外の診断を、共通処理とrenderer経由で確認する。"""

import json
from uuid import UUID

import pytest
import structlog

from app.analysis.assessment.events import (
    ArticleAssessedInScopeEvent,
    AssessedEventInvalidError,
)
from app.log_policy import BASE_LOG_RULES, PolicyLogger, build_processors, policy_logger
from app.log_policy.exceptions.application import convert_application_exception
from app.log_policy.exceptions.extraction import CAUSE_DEPTH_LIMIT, EXCEPTION_LIMIT

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
    """構築時にアプリ用変換を指定し、実際のJSON出力を返すロガーを作る。"""
    configure_chain()
    structlog.configure(
        processors=build_processors(
            structlog.processors.JSONRenderer(),
            exception_converter=convert_application_exception,
        ),
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
    assert "causes" not in output
    assert "synthetic-private" not in json.dumps(output)


def test_deepest_exception_keeps_issue_fields_and_codes(
    application_logger, validation_failure
) -> None:
    """探索の最深部でも、診断項目のfield・codeまで値準備を通過する。"""
    outer: BaseException = validation_failure
    for _ in range(CAUSE_DEPTH_LIMIT):
        parent = RuntimeError("operation failed")
        parent.__cause__ = outer
        outer = parent

    output = json.loads(application_logger.error("event_failed", exc_info=outer))

    for _ in range(CAUSE_DEPTH_LIMIT):
        output = output["causes"][0]
    assert output["error_details"] == {
        "kind": "application_validation",
        "reason": "invalid_payload",
        "issues": [
            {"field": "payload.curation_id", "code": "invalid_type"},
            {"field": "payload", "code": "unknown_field"},
        ],
    }


def test_application_details_use_shared_output_budget(
    application_logger, validation_failure
) -> None:
    """探索範囲内でも診断全体が共有予算を超えたら、ログを固定イベントへ置き換える。"""
    # グループ自身を含め探索上限内に収め、出力する診断の共有予算を検証する。
    group = ExceptionGroup("failures", [validation_failure] * (EXCEPTION_LIMIT - 1))

    output = json.loads(application_logger.error("event_failed", exc_info=group))

    assert output == {
        "event": "log_policy_budget_exceeded",
        "_policy_limited": True,
        "_policy_limit_reason": "value_count",
    }
