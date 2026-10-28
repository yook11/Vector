"""Service 層から送出されるドメイン例外。

各例外クラスは特定の HTTP ステータスコードに対応する。
Router 層でこれらを捕捉し HTTPException に変換する。
"""

from enum import StrEnum


class NotFoundResource(StrEnum):
    """404の応答で名指す対象の種類と、その応答の文。"""

    NEWS_ARTICLE = "News article not found"
    CATEGORY = "Category not found"
    NEWS_SOURCE = "News source not found"


class NotFoundError(Exception):
    """対象のリソースが存在しない → 404。"""

    # 応答の文は公開されるため、任意の文ではなく決まった種類だけを受け取る。
    def __init__(self, resource: NotFoundResource) -> None:
        self.resource = resource
        self.detail = resource.value
        super().__init__(self.detail)


class ReferenceNotFoundError(Exception):
    """参照先のエンティティが存在しない → 400。"""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class InvalidQueryError(Exception):
    """ユーザー入力が処理不能 → 422。"""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)
