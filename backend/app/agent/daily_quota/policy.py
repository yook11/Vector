"""ユーザー日次利用枠の固定 policy。"""

from zoneinfo import ZoneInfo

DAILY_REQUEST_LIMIT = 10
DAILY_QUOTA_TIMEZONE_NAME = "Asia/Tokyo"
DAILY_QUOTA_TIMEZONE = ZoneInfo(DAILY_QUOTA_TIMEZONE_NAME)
