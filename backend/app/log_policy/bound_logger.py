"""ログ出力の通常障害を業務処理へ伝播させない共通ラッパー。"""

import logging
from typing import Any

import structlog

_FilteringBoundLogger = structlog.make_filtering_bound_logger(logging.INFO)


class ApplicationBoundLogger(_FilteringBoundLogger):
    """processorの実行から出力までの通常障害を捕捉する。"""

    def _proxy_to_logger(
        self,
        method_name: str,
        event: str | None = None,
        **event_fields: Any,
    ) -> Any:
        try:
            return super()._proxy_to_logger(method_name, event, **event_fields)
        except Exception:
            return None
