"""補完工程の失敗と、その原因になった取得・抽出・構築の例外を表す。"""

from enum import StrEnum
from typing import ClassVar

from app.collection.domain.analyzable_article import QualityTooLow
from app.collection.response_size import ResponseSizeBasis
from app.shared.errors import ApplicationError


class ArticleCompletionError(ApplicationError):
    def __init__(self, *, cause: Exception) -> None:
        super().__init__("記事の補完工程で失敗しました")
        self.cause = cause
        self.__cause__ = cause


class ArticleBuildRejectedError(ApplicationError):
    CODE: ClassVar[str] = "article_invalid"

    def __init__(self, rejection: QualityTooLow) -> None:
        super().__init__(
            "記事が分析に必要な条件を満たしません",
            details={"defects": [defect.value for defect in rejection.defects]},
        )
        self.rejection = rejection


class FetchResource(StrEnum):
    ROBOTS_TXT = "robots_txt"
    ARTICLE_PAGE = "article_page"


class RobotsDisallowedError(ApplicationError):
    CODE: ClassVar[str] = "robots_disallowed"

    def __init__(self) -> None:
        super().__init__("robots.txtが記事の取得を禁止しています")


class ResponseSizeLimitExceededError(ApplicationError):
    CODE: ClassVar[str] = "response_too_large"

    def __init__(
        self,
        *,
        resource: FetchResource,
        limit_bytes: int,
        observed_bytes: int,
        size_basis: ResponseSizeBasis,
    ) -> None:
        super().__init__(
            "応答がサイズの上限を超えました",
            details={
                "resource": resource.value,
                "limit_bytes": limit_bytes,
                "observed_bytes": observed_bytes,
                "size_basis": size_basis.value,
            },
        )
        self.resource = resource
        self.limit_bytes = limit_bytes
        self.observed_bytes = observed_bytes
        self.size_basis = size_basis


class FetchDeadlineExceededError(ApplicationError):
    CODE: ClassVar[str] = "fetch_deadline_exceeded"

    def __init__(self, *, resource: FetchResource, limit_seconds: float) -> None:
        super().__init__(
            "取得が制限時間内に終わりませんでした",
            details={"resource": resource.value, "limit_seconds": limit_seconds},
        )
        self.resource = resource
        self.limit_seconds = limit_seconds
