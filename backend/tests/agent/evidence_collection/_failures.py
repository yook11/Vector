"""内部検索の失敗を、本番と同じ原因つきで組み立てるテスト用の部品。"""

from __future__ import annotations

from app.agent.evidence_collection.internal_search.contract import InternalSearchError
from app.ai_providers.errors import AIProviderTransportError
from app.db.errors import DatabaseTimeoutError, DatabaseTimeoutErrorReason
from app.http.errors import HttpTransportError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
)


def article_search_failed() -> InternalSearchError:
    """記事の検索がDBの時間切れで失敗したときの例外。"""
    return InternalSearchError(
        cause=DatabaseTimeoutError(reason=DatabaseTimeoutErrorReason.QUERY_CANCELED)
    )


def query_embedding_failed() -> InternalSearchError:
    """クエリのベクトル化がAIとの通信の失敗で止まったときの例外。"""
    return InternalSearchError(
        cause=AIProviderTransportError(
            http_error=HttpTransportError(
                failure=HttpTransportFailure(
                    HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
                )
            )
        )
    )
