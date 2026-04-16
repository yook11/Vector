"""Service 層から送出されるドメイン例外。

各例外クラスは特定の HTTP ステータスコードに対応する。
Router 層でこれらを捕捉し HTTPException に変換する。
"""


class NotFoundError(Exception):
    """対象のリソースが存在しない → 404。"""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class DuplicateError(Exception):
    """ユニーク制約違反になる → 409。"""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class ReferenceNotFoundError(Exception):
    """参照先のエンティティが存在しない → 400。"""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)
