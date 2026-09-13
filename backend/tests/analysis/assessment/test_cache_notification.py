"""記事保存結果からフロントエンドへのキャッシュ無効化通知を保証する。"""

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx  # noqa: TID251 (通知先は MockTransport で置換する)
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from app.analysis.assessment.domain.ready import (
    AssessmentReadyBuildRejected,
    AssessmentReadyBuildRejectionReason,
    ReadyForAssessment,
)
from app.analysis.assessment.service import (
    AssessmentCompletion,
    AssessmentCompletionKind,
)
from app.analysis.failure_handling import FailureHandlingDecision
from app.config import settings
from app.queue.messages.assessment import AssessmentTrigger
from app.queue.tasks.assessment import assess_content

_SECRET = "test-only-cache-notification-secret"
_TAGS = ["articles:list", "articles:categories"]


@dataclass
class NotificationCase:
    context: MagicMock
    save: AsyncMock
    ready: AsyncMock
    requests: list[httpx.Request] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    status_code: int = 200
    network_error: bool = False


@pytest.fixture
def notification_case(monkeypatch: pytest.MonkeyPatch) -> NotificationCase:
    """保存結果はスタブ化し、通知の組み立てからHTTP送信まで実装を通す。"""
    context = MagicMock()
    context.state = SimpleNamespace(
        session_factory=MagicMock(),
        assessor=MagicMock(),
    )
    context.message.labels = {"_retries": 0, "max_retries": 2}
    case = NotificationCase(
        context=context,
        save=AsyncMock(),
        ready=AsyncMock(
            return_value=(
                ReadyForAssessment(
                    curation_id=2, translated_title="title", summary="summary"
                ),
                7,
            )
        ),
    )

    async def save_successfully(*args, **kwargs) -> AssessmentCompletion:
        case.events.append("saved")
        return AssessmentCompletion(AssessmentCompletionKind.IN_SCOPE, 100)

    async def receive(request: httpx.Request) -> httpx.Response:
        case.events.append("notification")
        case.requests.append(request)
        if case.network_error:
            raise httpx.ConnectError("notification unavailable", request=request)
        return httpx.Response(case.status_code, json={"ok": case.status_code == 200})

    case.save.side_effect = save_successfully
    monkeypatch.setattr(
        "app.queue.tasks.assessment.ReadyForAssessment.try_advance_from", case.ready
    )
    monkeypatch.setattr(
        "app.queue.tasks.assessment.AssessmentService",
        MagicMock(return_value=SimpleNamespace(execute=case.save)),
    )
    monkeypatch.setattr(
        "app.queue.tasks.assessment.AssessmentFailureHandler",
        MagicMock(
            return_value=SimpleNamespace(
                handle=AsyncMock(return_value=FailureHandlingDecision(reraise=False))
            )
        ),
    )
    # 後続処理は置き換えるだけにし、このファイルの期待値には含めない。
    monkeypatch.setattr(
        "app.queue.tasks.assessment.generate_embedding.kiq", AsyncMock()
    )
    monkeypatch.setattr(settings, "internal_frontend_base_url", "http://frontend:3000")
    monkeypatch.setattr(settings, "revalidate_bearer_secret", SecretStr(_SECRET))
    monkeypatch.setattr(
        "app.shared.revalidate.make_internal_async_client",
        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(receive)),
    )
    return case


@pytest.mark.asyncio
async def test_saved_article_sends_authenticated_invalidation_request(
    notification_case: NotificationCase,
) -> None:
    """保存成功後に正しい宛先・認証・タグで一度だけ通知する。"""
    with capture_logs() as logs:
        await assess_content(
            trigger=AssessmentTrigger(curation_id=2), ctx=notification_case.context
        )

    assert notification_case.events == ["saved", "notification"]
    [request] = notification_case.requests
    assert request.method == "POST"
    assert str(request.url) == "http://frontend:3000/api/internal/revalidate"
    assert request.headers["authorization"] == f"Bearer {_SECRET}"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {"tags": _TAGS}
    assert any(log["event"] == "frontend_revalidate_ok" for log in logs)


@pytest.mark.asyncio
async def test_failed_article_save_does_not_send_invalidation_request(
    notification_case: NotificationCase,
) -> None:
    """保存失敗時はキャッシュ無効化通知を送らない。"""
    notification_case.save.side_effect = RuntimeError("commit failed")

    await assess_content(
        trigger=AssessmentTrigger(curation_id=2), ctx=notification_case.context
    )

    assert notification_case.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [AssessmentCompletionKind.OUT_OF_SCOPE, AssessmentCompletionKind.ALREADY_ASSESSED],
)
async def test_no_published_article_does_not_send_invalidation_request(
    notification_case: NotificationCase,
    kind: AssessmentCompletionKind,
) -> None:
    """対象外判定や競合で新しい公開記事が保存されなければ通知しない。"""
    notification_case.save.side_effect = None
    notification_case.save.return_value = AssessmentCompletion(kind)

    await assess_content(
        trigger=AssessmentTrigger(curation_id=2), ctx=notification_case.context
    )

    assert notification_case.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        AssessmentReadyBuildRejectionReason.ALREADY_IN_SCOPE,
        AssessmentReadyBuildRejectionReason.ALREADY_OUT_OF_SCOPE,
    ],
)
async def test_already_assessed_article_does_not_send_invalidation_request(
    notification_case: NotificationCase, code: AssessmentReadyBuildRejectionReason
) -> None:
    """解析済みの記事への重複トリガーでは通知しない。"""
    notification_case.ready.return_value = AssessmentReadyBuildRejected(
        code, analyzable_article_id=7
    )

    await assess_content(
        trigger=AssessmentTrigger(curation_id=2), ctx=notification_case.context
    )

    assert notification_case.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "network"])
async def test_failed_notification_is_logged_as_failure_not_success(
    notification_case: NotificationCase, failure: str
) -> None:
    """通知先のHTTPエラーや接続失敗を通知成功として記録しない。"""
    notification_case.status_code = 500
    notification_case.network_error = failure == "network"

    with capture_logs() as logs:
        await assess_content(
            trigger=AssessmentTrigger(curation_id=2), ctx=notification_case.context
        )

    assert len(notification_case.requests) == 1
    assert any(log["event"] == "frontend_revalidate_failed" for log in logs)
    assert not any(log["event"] == "frontend_revalidate_ok" for log in logs)
