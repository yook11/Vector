"""実handlerが指定記事の判定結果を原子的に確定することを確認する。"""

import json

import pytest

from local_tests.assessment.support import (
    deepseek_reply,
    fetch_stored_assessment,
    invoke_event,
    seed_curation,
)


@pytest.mark.asyncio
async def test_in_scope_assessment_completes_successfully(
    system_database, assessment_runtime, deepseek_response
):
    """対象内と判定された記事の結果・成功監査・後続Outboxが正しく保存される。"""
    other = await seed_curation(
        system_database,
        "https://example.com/other",
        title="別記事タイトル",
        summary="別記事だけの要約",
    )
    target = await seed_curation(
        system_database,
        "https://example.com/target",
        title="対象記事タイトル",
        summary="対象記事だけの要約",
    )
    expected_key_points = [
        {
            "content": "半導体の新製品を発表",
            "mentions": [{"surface": "製品A", "type": "product"}],
        }
    ]
    deepseek_response.return_value = deepseek_reply(
        category="semiconductor",
        investor_take="対象記事への投資判断",
        key_points=expected_key_points,
    )

    response = await invoke_event(target)

    assert response == {"batchItemFailures": []}

    # AIへ渡った本文が、イベントで指定した記事のものかを確認する。
    deepseek_response.assert_awaited_once()
    ai_request = deepseek_response.await_args.args[0]
    ai_request_body = json.loads(ai_request.content)
    assessment_input = ai_request_body["messages"][0]["content"]
    assert "対象記事だけの要約" in assessment_input
    assert "別記事だけの要約" not in assessment_input

    saved = await fetch_stored_assessment(system_database, target.curation_id)
    assert len(saved.in_scope) == 1
    saved_article = saved.in_scope[0].copy()
    # DBの採番値は固定せず、後続Outboxが同じ記事を指すことを確認する。
    saved_article_id = saved_article.pop("id")
    assert saved_article == {
        "translated_title": "対象記事タイトル",
        "summary": "対象記事だけの要約",
        "category": "semiconductor",
        "investor_take": "対象記事への投資判断",
        "key_points": expected_key_points,
    }
    assert saved.out_of_scope == []

    assert len(saved.audits) == 1
    success_audit = saved.audits[0]
    assert {
        "event_type": success_audit["event_type"],
        "article_id": success_audit["article_id"],
    } == {
        "event_type": "succeeded",
        "article_id": target.analyzable_article_id,
    }

    assert len(saved.outbox) == 1
    outbox_event = saved.outbox[0]
    assert {
        "event_type": outbox_event["event_type"],
        "payload": outbox_event["payload"],
    } == {
        "event_type": "article.assessed_in_scope",
        "payload": {
            "curation_id": target.curation_id,
            "analyzed_article_id": saved_article_id,
        },
    }

    # 処理対象ではない記事には、結果・監査・Outboxを追加しない。
    unchanged = await fetch_stored_assessment(system_database, other.curation_id)
    assert unchanged.in_scope == []
    assert unchanged.out_of_scope == []
    assert unchanged.audits == []
    assert unchanged.outbox == []


@pytest.mark.asyncio
async def test_out_of_scope_event_saves_result_without_outbox(
    system_database, assessment_runtime, deepseek_response
):
    """対象外結果と成功監査だけを確定し、Embedding向けOutboxを発行しない。"""
    target = await seed_curation(system_database, "https://example.com/out-of-scope")
    points = [{"content": "投資対象外の催し", "mentions": []}]
    deepseek_response.return_value = deepseek_reply(
        category="out_of_scope", investor_take="対象外とする理由", key_points=points
    )

    response = await invoke_event(target)

    assert response == {"batchItemFailures": []}
    saved = await fetch_stored_assessment(system_database, target.curation_id)
    assert saved.in_scope == []
    assert len(saved.out_of_scope) == 1
    assert saved.out_of_scope[0] == {
        "id": saved.out_of_scope[0]["id"],
        "translated_title": "対象タイトル",
        "summary": "対象要約",
        "investor_take": "対象外とする理由",
        "key_points": points,
    }
    assert len(saved.audits) == 1
    assert saved.audits[0]["event_type"] == "succeeded"
    assert saved.audits[0]["article_id"] == target.analyzable_article_id
    assert saved.outbox == []


@pytest.mark.asyncio
async def test_database_failure_rolls_back_result_audit_and_outbox(
    system_database, assessment_runtime, database_error_before_commit
):
    """3種類の実INSERT後のDB障害で全てを戻し、失敗監査だけを別トランザクションで確定する。"""
    target = await seed_curation(system_database, "https://example.com/rollback")

    response = await invoke_event(target)

    assert database_error_before_commit.pending_counts == [(1, 1, 1)]
    assert len(database_error_before_commit.errors) == 1
    assert response == {
        "batchItemFailures": [{"itemIdentifier": str(target.curation_id)}]
    }
    saved = await fetch_stored_assessment(system_database, target.curation_id)
    assert saved.in_scope == saved.out_of_scope == saved.outbox == []
    assert len(saved.audits) == 1
    assert saved.audits[0]["event_type"] == "failed"
    assert saved.audits[0]["article_id"] == target.analyzable_article_id
