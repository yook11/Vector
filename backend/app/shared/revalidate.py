"""生成成功後に frontend のキャッシュ無効化 endpoint を叩く汎用 notifier。

複数の insights 機能 (briefing / trend discovery) が同じ仕組みで frontend の
``"use cache"`` tag を on-demand revalidate する。tag の中身は機能固有なので、
ここは「tag のリストを POST する」transport だけを持ち、どの tag を打つかは
呼び出し側が決める (shared kernel には BC 固有概念を置かない)。

失敗時は ``warn`` ログ降格 (raise しない、``feedback_failure_visibility.md``):
- DB には生成物が保存済 = ビジネス価値達成済
- キャッシュは ``cacheLife`` でも失効するが、表示中画面の新着通知の再送は保証しない
- raise すると task retry → 生成 (LLM 呼出等) の重複で害が大きい
- 「降格」であって「握り潰し」ではない (warn ログで運用に見える)

通信先は compose 内部 DNS (``http://frontend:3000``) や実行基盤の内部 namespace
(``*.vector.internal``) で、自分たちの deployment のコンテナ宛。
よって ``make_internal_async_client`` を使う。

宛先は既存アプリのSettingsで制限し、Assessment LambdaではTerraformが
Cloud Mapのfrontend URLを設定するため、利用者入力から組み立てない。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Protocol

from pydantic import SecretStr

from app.audit.error_fields import exception_fqn
from app.http.internal import make_internal_async_client
from app.log_policy.policies.cache_revalidation import CACHE_REVALIDATION_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger

if TYPE_CHECKING:
    from app.config import Settings


class RevalidateNotifier(Protocol):
    """notify 1 メソッドだけを持つ抽象 (CLI 用 Null 差し替え用)。"""

    async def notify(self, *, tags: Sequence[str]) -> None: ...


class FrontendRevalidateNotifier:
    """指定された cache tag 群を frontend に revalidate させる。"""

    def __init__(
        self,
        *,
        frontend_base_url: str,
        secret_provider: Callable[[], Awaitable[SecretStr]],
    ) -> None:
        self._url = f"{frontend_base_url.rstrip('/')}/api/internal/revalidate"
        self._secret_provider = secret_provider

    @classmethod
    def from_settings(cls, settings: Settings) -> FrontendRevalidateNotifier:
        """config の internal frontend URL / bearer secret から構築する。"""

        async def secret_provider() -> SecretStr:
            return settings.revalidate_bearer_secret

        return cls(
            frontend_base_url=settings.internal_frontend_base_url,
            secret_provider=secret_provider,
        )

    async def notify(self, *, tags: Sequence[str]) -> None:
        logger = create_policy_json_logger(__name__, CACHE_REVALIDATION_LOG_RULES)
        operation = "get_secret"
        try:
            secret = await self._secret_provider()
            operation = "notify"
            async with make_internal_async_client(timeout=5.0) as client:
                resp = await client.post(
                    self._url,
                    json={"tags": list(tags)},
                    headers={"Authorization": f"Bearer {secret.get_secret_value()}"},
                )
                resp.raise_for_status()
            logger.info("frontend_revalidate_ok", tags=list(tags))
        except Exception as exc:
            logger.warning(
                "frontend_revalidate_failed",
                tags=list(tags),
                operation=operation,
                error_class=exception_fqn(exc),
            )


class NullRevalidateNotifier:
    """no-op notifier。本番 frontend を叩きたくない経路用の差し替え。"""

    async def notify(self, *, tags: Sequence[str]) -> None:
        return None
