"""``classify_content`` (deprecated alias) と ``assess_content`` (新 task) の検証。

PR3.5-d.0 deploy 時点で broker queue に残った in-flight ``classify_content``
message を消化するため、旧 task name と新 task name の **2 つを同 logic に向ける**
alias 構造を採用した (spec ``specs/stage4-assessment-rename.md`` §7)。

このテストの存在自体が「alias は意図的な暫定措置」のドキュメントになる。
削除条件 (broker queue / DLQ / 24h alias 不発火 すべて満たした時) を確認した
あとに、別 PR (PR3.5-d.3) で alias 関数 + 本テスト + producer 互換コードを削除する。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.assessment.domain.ready import ReadyForAssessment
from app.analysis.tasks import assess_content, classify_content


def _make_ready(extraction_id: int = 7) -> ReadyForAssessment:
    return ReadyForAssessment(
        extraction_id=extraction_id,
        translated_title="title",
        summary="summary",
    )


def _make_ctx() -> MagicMock:
    """taskiq Context モック (alias のロギングとフォワードのみが対象)。"""
    ctx = MagicMock()
    ctx.state = SimpleNamespace(
        session_factory=MagicMock(),
        classifier=MagicMock(MODEL="m", RPM=None, RPD=None),
    )
    ctx.message.labels = {"retry_count": 0, "max_retries": 0}
    return ctx


# ---------------------------------------------------------------------------
# broker への二重登録 (新 task + 互換 alias)
# ---------------------------------------------------------------------------


def test_assess_content_registered_with_new_task_name() -> None:
    """新 task は ``assess_content`` という task_name で broker に登録される。"""
    assert assess_content.task_name == "assess_content"


def test_classify_content_alias_registered_with_legacy_task_name() -> None:
    """alias は旧 ``classify_content`` の task_name のままで broker に登録される。

    in-flight message (旧名で enqueue 済) を消化するためにこの登録が必要。
    """
    assert classify_content.task_name == "classify_content"


# ---------------------------------------------------------------------------
# alias の forward 動作: classify_content(...) は assess_content(...) を呼ぶ
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alias_forwards_to_assess_content() -> None:
    """alias 経由の invoke が新 logic (assess_content) を await する。"""
    ready = _make_ready(extraction_id=42)
    ctx = _make_ctx()

    with patch(
        "app.analysis.tasks.assess_content",
        new=AsyncMock(),
    ) as mock_assess:
        await classify_content(ready=ready, ctx=ctx)

    mock_assess.assert_awaited_once_with(ready, ctx)


# ---------------------------------------------------------------------------
# alias の logfire signal: drain 終了判定の根拠を残す
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alias_emits_deprecation_signal_on_invoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """alias 経由の invoke で ``classify_content_alias_invoked`` が記録される。

    本イベントは PR3.5-d.3 の alias 削除判定 (24 時間内 0 件) の logfire signal。
    structlog は標準 logging を経由しないため caplog では捕捉できず、本テストは
    ``logger.info`` を spy で差し替えて event 名を検証する。
    """
    from app.analysis import tasks as tasks_module

    captured: list[tuple[str, dict[str, object]]] = []

    def spy_info(event: str, **kwargs: object) -> None:
        captured.append((event, kwargs))

    monkeypatch.setattr(tasks_module.logger, "info", spy_info)

    ready = _make_ready(extraction_id=99)
    ctx = _make_ctx()
    with patch("app.analysis.tasks.assess_content", new=AsyncMock()):
        await classify_content(ready=ready, ctx=ctx)

    assert any(event == "classify_content_alias_invoked" for event, _ in captured)


# ---------------------------------------------------------------------------
# taskiq in-flight message 互換: ReadyForAssessment field 構造
# ---------------------------------------------------------------------------


def test_ready_for_assessment_field_shape_matches_legacy() -> None:
    """taskiq wire format は Pydantic ``model_dump_json`` の field 構造ベース。

    旧 ``ReadyForClassification`` で enqueue 済の message を新型として deserialize
    するには field 名・型・順序が完全一致している必要がある (本 PR の最重要 invariant)。

    field 構造が崩れたら、broker queue に残った旧 message が deserialize 不能
    になり、worker が静かに dead-letter queue に流す事故が起きる。
    """
    assert set(ReadyForAssessment.model_fields) == {
        "extraction_id",
        "translated_title",
        "summary",
    }
