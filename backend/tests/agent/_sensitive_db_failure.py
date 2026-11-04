"""秘匿値をSQLのパラメータとDETAILに持つDB例外と、その秘匿値が出力に無いことの確認。"""

from __future__ import annotations

from sqlalchemy.exc import DBAPIError

from tests.conftest import TEST_USER_ID

SENSITIVE_MARKERS = (
    TEST_USER_ID,
    "SECRET_SQL_MARKER",
    "SECRET_QUESTION_MARKER",
    "SECRET_ANSWER_MARKER",
    "SECRET_PROVIDER_RAW_MARKER",
    "parameters:",
)


class SensitivePersistenceFailure(DBAPIError):
    """秘匿値をSQLのパラメータとDETAILに持つ、コミット時のDB例外。"""

    def __init__(self) -> None:
        super().__init__(
            "UPDATE agent_runs SET status = $1 WHERE user_id = $2",
            {
                "user_id": TEST_USER_ID,
                "question": "SECRET_QUESTION_MARKER",
                "answer": "SECRET_ANSWER_MARKER",
                "provider_raw": "SECRET_PROVIDER_RAW_MARKER",
            },
            RuntimeError(
                "asyncpg failure\n"
                "DETAIL:  Key (question)=(SECRET_QUESTION_MARKER) SECRET_SQL_MARKER"
            ),
        )


def assert_no_sensitive_marker(output: object) -> None:
    rendered = repr(output)
    assert [marker for marker in SENSITIVE_MARKERS if marker in rendered] == []
