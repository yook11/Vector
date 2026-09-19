"""表現が変わっても保護対象を最終ログへ残さない。"""

import json
from functools import partial

import pytest
import structlog

from app.log_policy import (
    BASE_LOG_RULES,
    build_processors,
    create_policy_logger,
    policy_logger,
)
from app.log_policy.policies.ai_inference import AI_INFERENCE_LOG_RULES

pytestmark = pytest.mark.unit


@pytest.fixture(params=["json", "console"])
def rendered_logger(request, configure_chain):
    """本物のチェーンとrendererを通した出力を外部送信せず受け取る。"""
    configure_chain()
    renderer = (
        structlog.processors.JSONRenderer()
        if request.param == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=build_processors(renderer),
        logger_factory=partial(
            create_policy_logger, output_logger_factory=structlog.ReturnLoggerFactory()
        ),
    )
    return partial(policy_logger, "mask_test")


@pytest.fixture
def article_logger(rendered_logger):
    """本文を保護する目的ポリシーの代表としてAI推論の規則を適用する。"""
    return rendered_logger(AI_INFERENCE_LOG_RULES)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"content": ["PRIVATE_ONE", "PRIVATE_TWO"]}', '{"content": ***}'),
        (
            '{"content": {"first": "PRIVATE_ONE", "second": "PRIVATE_TWO"}}',
            '{"content": ***}',
        ),
    ],
)
def test_json_article_value_is_masked_after_rendering(article_logger, text, expected):
    """JSON文字列の本文値全体が消え、後続の診断が残る。"""
    output = article_logger.info(text + " diagnostic_kept")
    assert "PRIVATE_ONE" not in output
    assert "PRIVATE_TWO" not in output
    assert "diagnostic_kept" in output
    assert "***" in output
    if output.startswith("{"):
        assert json.loads(output)["event"] == expected + " diagnostic_kept"


@pytest.mark.parametrize(
    "text",
    [
        "content=['PRIVATE_ONE', 'PRIVATE_TWO'] diagnostic_kept",
        "content={'first': 'PRIVATE_ONE', 'second': 'PRIVATE_TWO'} diagnostic_kept",
        "content=('PRIVATE_ONE', 'PRIVATE_TWO') diagnostic_kept",
    ],
)
def test_repr_article_value_is_masked_after_rendering(article_logger, text):
    """Python表現の本文値全体が消え、前後の診断が残る。"""
    output = article_logger.info("failed " + text)
    assert "PRIVATE_ONE" not in output
    assert "PRIVATE_TWO" not in output
    assert "failed content=*** diagnostic_kept" in output


def test_article_container_in_exception_is_masked_after_rendering(article_logger):
    """例外文内の本文配列も最終出力へ漏れず、例外の診断が残る。"""
    output = article_logger.error(
        "failed",
        exc_info=ValueError("content=['PRIVATE_ONE', 'PRIVATE_TWO'] diagnostic_kept"),
    )
    assert "PRIVATE_ONE" not in output
    assert "PRIVATE_TWO" not in output
    assert "content=*** diagnostic_kept" in output
    assert "builtins.ValueError" in output


def test_broken_article_container_in_exception_hides_remainder(article_logger):
    """壊れた本文値は末尾まで伏せ、値より前の原因説明は残す。"""
    output = article_logger.error(
        "failed",
        exc_info=ValueError("diagnostic_kept content=['PRIVATE_ONE', 'PRIVATE_TWO'"),
    )
    assert "PRIVATE_ONE" not in output
    assert "PRIVATE_TWO" not in output
    assert "diagnostic_kept content=***" in output
    assert "builtins.ValueError" in output


def test_password_container_in_exception_is_masked_after_rendering(rendered_logger):
    """基底ポリシーでも例外文内の認証情報配列全体を伏せる。"""
    logger = rendered_logger(BASE_LOG_RULES)
    output = logger.error(
        "failed", exc_info=ValueError("password=['SECRET_ONE', 'SECRET_TWO'] host=db")
    )
    assert "SECRET_ONE" not in output
    assert "SECRET_TWO" not in output
    assert "password=*** host=db" in output
    assert "builtins.ValueError" in output
