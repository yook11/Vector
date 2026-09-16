"""対象内の記事を保存した後、一覧とカテゴリーの更新を通知する。"""

from app.shared.revalidate import RevalidateNotifier


class ArticleListUpdateNotifier:
    def __init__(self, transport: RevalidateNotifier) -> None:
        self._transport = transport

    async def notify_article_list_updated(self) -> None:
        await self._transport.notify(tags=["articles:list", "articles:categories"])
