"""回答生成と未完了run回収で共有する時間定義。"""

from datetime import timedelta

ANSWER_GENERATION_TIMEOUT_SECONDS = 15
ANSWER_GENERATION_RECOVERY_GRACE_SECONDS = 30


def answer_generation_recovery_window() -> timedelta:
    return timedelta(
        seconds=(
            ANSWER_GENERATION_TIMEOUT_SECONDS + ANSWER_GENERATION_RECOVERY_GRACE_SECONDS
        )
    )
