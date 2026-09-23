"""実handlerが判定結果を監査・Outboxとともに確定することを確認する。"""

import pytest

from local_tests.curation.support import (
    curation_reply,
    fetch_stored_curation,
    invoke_event,
    seed_article,
)


@pytest.mark.asyncio
async def test_signal_article_saves_curation_audit_and_outbox(
    system_database, curation_runtime, gemini_response
):
    """signalの結果・成功監査・Assessment向けOutboxを確定し、noiseは保存しない。"""
    target = await seed_article(
        system_database,
        "https://example.com/signal",
        title="Chipmaker unveils new accelerator",
        content="The chipmaker announced a new accelerator for data centers.",
    )
    gemini_response.return_value = curation_reply(
        relevance="signal",
        title_ja="半導体メーカーが新型アクセラレーターを発表",
        summary_ja="データセンター向けの新型アクセラレーターを発表した。",
    )

    response = await invoke_event(target)

    assert response == {"batchItemFailures": []}
    saved = await fetch_stored_curation(system_database, target.analyzable_article_id)
    assert len(saved.curations) == 1
    curation = saved.curations[0]
    assert curation == {
        "id": curation["id"],
        "translated_title": "半導体メーカーが新型アクセラレーターを発表",
        "summary": "データセンター向けの新型アクセラレーターを発表した。",
    }
    assert saved.noises == []
    assert saved.audits == [
        {"event_type": "succeeded", "outcome_code": "curated_signal"}
    ]
    assert saved.outbox_payloads == [
        {
            "curation_id": curation["id"],
            "analyzable_article_id": target.analyzable_article_id,
        }
    ]


@pytest.mark.asyncio
async def test_noise_article_saves_noise_and_audit_without_outbox(
    system_database, curation_runtime, gemini_response
):
    """noiseの結果と成功監査だけを確定し、Assessment向けOutboxを発行しない。"""
    target = await seed_article(
        system_database,
        "https://example.com/noise",
        title="Local bakery opens second shop",
        content="A local bakery opened its second shop downtown.",
    )
    gemini_response.return_value = curation_reply(
        relevance="noise",
        title_ja="地元のパン屋が2号店を開店",
        summary_ja="地元のパン屋が中心街に2号店を開いた。",
    )

    response = await invoke_event(target)

    assert response == {"batchItemFailures": []}
    saved = await fetch_stored_curation(system_database, target.analyzable_article_id)
    assert saved.curations == []
    assert saved.noises == [
        {
            "translated_title": "地元のパン屋が2号店を開店",
            "summary": "地元のパン屋が中心街に2号店を開いた。",
        }
    ]
    assert saved.audits == [
        {"event_type": "succeeded", "outcome_code": "curated_noise"}
    ]
    assert saved.outbox_payloads == []
