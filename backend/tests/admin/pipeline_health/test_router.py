"""GET /api/v1/admin/pipeline/health のルーターテスト (認可 + レスポンス形)。"""

from httpx import AsyncClient

from app.audit.domain.event import Stage

_HEALTH_URL = "/api/v1/admin/pipeline/health"

# 監査 Stage enum の定義順が API 表示順。
_EXPECTED_STAGES = [stage.value for stage in Stage]
_SUMMARY_KEYS = {
    "failedEventCount24h",
    "backfillTargetTotal",
    "oldestBackfillTargetAgeSeconds",
    "completionQueueCount",
    "oldestCompletionQueueAgeSeconds",
    "observedAt",
    "eventWindowStart",
}
_STAGE_KEYS = {
    "stage",
    "succeededEventCount24h",
    "failedEventCount24h",
    "queueCount",
    "oldestQueueAgeSeconds",
    "backfillTargetCount",
    "oldestBackfillTargetAgeSeconds",
    "lastSucceededAt",
}


async def test_pipeline_health_requires_auth(client: AsyncClient) -> None:
    """未認証アクセスは 401。"""
    response = await client.get(_HEALTH_URL)
    assert response.status_code == 401


async def test_pipeline_health_forbidden_for_non_admin(
    authed_client: AsyncClient,
) -> None:
    """一般ユーザーは admin 依存で 403。"""
    response = await authed_client.get(_HEALTH_URL)
    assert response.status_code == 403


async def test_pipeline_health_ok_for_admin(admin_client: AsyncClient) -> None:
    """admin は 200。空 DB でも全 audit stage が定義順で返る。"""
    response = await admin_client.get(_HEALTH_URL)
    assert response.status_code == 200
    body = response.json()
    assert [s["stage"] for s in body["stages"]] == _EXPECTED_STAGES


async def test_pipeline_health_response_uses_camel_case(
    admin_client: AsyncClient,
) -> None:
    """summary / stage が camelCase キー (24h は小文字 h) を持つ。"""
    response = await admin_client.get(_HEALTH_URL)
    body = response.json()
    assert set(body["summary"]) == _SUMMARY_KEYS
    assert set(body["stages"][0]) == _STAGE_KEYS
