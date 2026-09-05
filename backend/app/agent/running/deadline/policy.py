"""受付時に固定するRunの受理期限。"""

from datetime import datetime, timedelta

RUN_DEADLINE_SECONDS = 60


def deadline_for_run(created_at: datetime) -> datetime:
    return created_at + timedelta(seconds=RUN_DEADLINE_SECONDS)
