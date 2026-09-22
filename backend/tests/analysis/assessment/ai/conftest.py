"""AssessmentのAI呼び出しへ渡す実際の記事分析ロガー。"""

import pytest

from app.analysis.logging import create_article_analysis_logger


@pytest.fixture
def make_assessment_logger():
    def create():
        return create_article_analysis_logger().bind(
            stage="assessment", request_id="request-001", message_id="message-001"
        )

    return create
