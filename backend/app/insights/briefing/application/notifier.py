"""Briefing 生成成功後に frontend のキャッシュ無効化 endpoint を叩く。

責務分離: 永続化 (DB) は ``WeeklyBriefingService`` の責務、
キャッシュ通知はこの notifier に切り出す (`feedback_responsibility_by_purpose.md`)。

失敗時は ``warn`` ログ降格 (raise しない、`feedback_failure_visibility.md`):
- DB に briefing は保存済 = ビジネス価値達成済
- frontend には ISR backstop (1 時間) があるため、最大 1 時間以内には
  必ずキャッシュが更新される
- raise すると taskiq retry → DeepSeek 重複呼出で害が大きい
- 「降格」であって「握り潰し」ではない (warn ログで運用に見える)

通信先は compose 内部 DNS (``http://frontend:3000``) を想定するため、
SSRF guard 入りの ``make_safe_async_client`` (private IP を弾く) は使わず、
``httpx.AsyncClient`` を直接構築する。internal 通信専用。
"""

from __future__ import annotations

from typing import Protocol

import httpx  # noqa: TID251 (internal 通信専用、SSRF guard は不要)
import structlog

logger = structlog.get_logger(__name__)


class BriefingNotifier(Protocol):
    """notify 1 メソッドだけを持つ抽象 (CLI 用 NullNotifier 差し替え用)。"""

    async def notify(self, *, category_slug: str) -> None: ...


class FrontendRevalidateNotifier:
    """category_slug ごとの cache tag を frontend に revalidate させる。"""

    def __init__(self, *, frontend_base_url: str, secret: str) -> None:
        self._url = f"{frontend_base_url.rstrip('/')}/api/internal/revalidate"
        self._secret = secret

    async def notify(self, *, category_slug: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:  # noqa: TID251
                resp = await client.post(
                    self._url,
                    json={
                        "tags": [
                            f"briefing:{category_slug}",
                            "briefing:list",
                        ]
                    },
                    headers={"Authorization": f"Bearer {self._secret}"},
                )
                resp.raise_for_status()
            logger.info("briefing_revalidate_ok", category_slug=category_slug)
        except Exception as exc:
            logger.warning(
                "briefing_revalidate_failed",
                category_slug=category_slug,
                error=str(exc),
            )


class NullBriefingNotifier:
    """no-op notifier。CLI で本番 frontend を叩かないようにするための差し替え。"""

    async def notify(self, *, category_slug: str) -> None:
        return None
