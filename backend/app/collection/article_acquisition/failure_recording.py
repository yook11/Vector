"""取得失敗と変換棄却を、配送方式に依存せず監査へ記録する。"""

from __future__ import annotations

from app.audit.error_fields import exception_fqn
from app.audit.metrics import record_audit_dropped
from app.audit.stages.acquisition import SourceAcquisitionAuditRepository
from app.collection.article_acquisition.consumer_result import (
    NoRetryAcquisition,
    RetryAcquisition,
)
from app.collection.article_acquisition.fetched_article_converter import (
    AcquisitionConversionRejection,
)
from app.collection.article_acquisition.metrics import (
    AcquisitionEntryOutcome,
    record_acquisition_outcome,
)
from app.db.session import SessionFactory
from app.log_policy.policies.external_content import EXTERNAL_CONTENT_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger


class ArticleAcquisitionFailureRecorder:
    """ソースの取得失敗と記事単位の変換棄却の記録を担う。"""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def record_source_failure(
        self,
        *,
        source_id: int | None,
        source_name: str | None,
        failure: RetryAcquisition | NoRetryAcquisition,
    ) -> None:
        """ソースの取得失敗と再配信の判断を、別セッションで監査する。"""
        try:
            await self._audit_failure(source_id, source_name, failure)
        except Exception:  # noqa: S110
            # 監査・診断の通常障害で元の取得失敗を置き換えない。
            pass

    async def record_conversion_rejected(
        self,
        source_id: int,
        rej: AcquisitionConversionRejection,
    ) -> None:
        """entry 単位の変換棄却を別 session で best-effort 監査し metric も出す。

        rejected の永続化境界はこの別 tx commit なので、監査行・redacted log・
        ``vector.acquisition.outcome{rejected}`` の 3 面をここで完結させる。metric は
        commit 成功時にだけ +1 し、監査行と件数を構造的に揃える (監査を best-effort で
        握りつぶした場合は計上せず redacted log に退避する)。
        """
        try:
            async with self._session_factory() as audit_session:
                await SourceAcquisitionAuditRepository(
                    audit_session
                ).append_conversion_rejected(
                    source_id=source_id,
                    rejection=rej,
                )
                await audit_session.commit()
        except Exception as audit_exc:
            create_policy_json_logger(__name__, EXTERNAL_CONTENT_LOG_RULES).warning(
                "fetched_article_conversion_audit_dropped",
                operation="audit",
                source_id=source_id,
                business_outcome_code=rej.outcome_code,
                business_error_class=(
                    exception_fqn(rej.cause) if rej.cause is not None else None
                ),
                exc_info=audit_exc,
            )
            record_audit_dropped(SourceAcquisitionAuditRepository.STAGE)
            return
        # metric add を try の外に置き、counter 失敗を監査 drop と混同しない。
        record_acquisition_outcome(AcquisitionEntryOutcome.REJECTED, count=1)

    async def _audit_failure(
        self,
        source_id: int | None,
        source_name: str | None,
        failure: RetryAcquisition | NoRetryAcquisition,
    ) -> None:
        """best-effort failure audit。失敗時は監査の例外をログに退避する。"""
        exc = failure.error if isinstance(failure, RetryAcquisition) else failure.cause
        if not isinstance(exc, Exception):
            raise ValueError("取得不要の結果を失敗監査へ記録できません")
        try:
            async with self._session_factory() as session:
                await SourceAcquisitionAuditRepository(session).append_failure(
                    source_id=source_id,
                    source_name=source_name,
                    failure=failure,
                )
                await session.commit()
        except Exception as audit_exc:
            # 業務の例外は終端ログに出るため、ここでは型名だけを添える。
            create_policy_json_logger(__name__, EXTERNAL_CONTENT_LOG_RULES).warning(
                "source_acquisition_failure_audit_dropped",
                operation="audit",
                source_id=source_id,
                business_error_class=exception_fqn(exc),
                exc_info=audit_exc,
            )
            record_audit_dropped(SourceAcquisitionAuditRepository.STAGE)
