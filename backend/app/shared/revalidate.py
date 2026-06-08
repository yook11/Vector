"""生成成功後に frontend のキャッシュ無効化 endpoint を叩く汎用 notifier。

複数の insights 機能 (briefing / trend discovery) が同じ仕組みで frontend の
``"use cache"`` tag を on-demand revalidate する。tag の中身は機能固有なので、
ここは「tag のリストを POST する」transport だけを持ち、どの tag を打つかは
呼び出し側が決める (shared kernel には BC 固有概念を置かない)。

失敗時は ``warn`` ログ降格 (raise しない、``feedback_failure_visibility.md``):
- DB には生成物が保存済 = ビジネス価値達成済
- frontend には ``cacheLife`` の ISR backstop があり、最大 TTL 内には必ず更新される
- raise すると task retry → 生成 (LLM 呼出等) の重複で害が大きい
- 「降格」であって「握り潰し」ではない (warn ログで運用に見える)

通信先は compose 内部 DNS (``http://frontend:3000``) や Fly private network
(``*.flycast``) を想定するため、SSRF guard 入りの ``make_safe_async_client``
(private IP を弾く) は使わず、``httpx.AsyncClient`` を直接構築する。internal 通信専用。

宛先 host は config 層の ``internal_frontend_base_url`` validator
(``app/config.py``) で allowlist 制約済 = ここに渡る時点で宛先は定義上 internal。
よって raw httpx でも REVALIDATE_BEARER_SECRET が攻撃者ホストに送られる経路は
構造的に閉じている。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

import httpx  # noqa: TID251 (internal 通信専用、SSRF guard は不要)
import structlog

if TYPE_CHECKING:
    from app.config import Settings

logger = structlog.get_logger(__name__)


class RevalidateNotifier(Protocol):
    """notify 1 メソッドだけを持つ抽象 (CLI 用 Null 差し替え用)。"""

    async def notify(self, *, tags: Sequence[str]) -> None: ...


class FrontendRevalidateNotifier:
    """指定された cache tag 群を frontend に revalidate させる。"""

    def __init__(self, *, frontend_base_url: str, secret: str) -> None:
        self._url = f"{frontend_base_url.rstrip('/')}/api/internal/revalidate"
        self._secret = secret

    @classmethod
    def from_settings(cls, settings: Settings) -> FrontendRevalidateNotifier:
        """config の internal frontend URL / bearer secret から構築する。"""
        return cls(
            frontend_base_url=settings.internal_frontend_base_url,
            secret=settings.revalidate_bearer_secret.get_secret_value(),
        )

    async def notify(self, *, tags: Sequence[str]) -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:  # noqa: TID251
                resp = await client.post(
                    self._url,
                    json={"tags": list(tags)},
                    headers={"Authorization": f"Bearer {self._secret}"},
                )
                resp.raise_for_status()
            logger.info("frontend_revalidate_ok", tags=list(tags))
        except Exception as exc:
            logger.warning(
                "frontend_revalidate_failed",
                tags=list(tags),
                error=str(exc),
            )


class NullRevalidateNotifier:
    """no-op notifier。本番 frontend を叩きたくない経路用の差し替え。"""

    async def notify(self, *, tags: Sequence[str]) -> None:
        return None
