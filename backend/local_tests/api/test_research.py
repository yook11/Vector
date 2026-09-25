"""リサーチのAPIが、vector_apiの権限で利用者の操作に対応する履歴を読み書きする。"""

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from local_tests.api.support import (
    daily_quota_counts,
    research_row_counts,
    seed_answer,
    seed_daily_quota,
    seed_external_source,
    seed_question,
    seed_thread,
    thread_record,
    thread_updated_at,
)

pytestmark = pytest.mark.asyncio

QUOTA_DATE = date(2026, 9, 20)


async def test_asking_in_new_thread_saves_question_and_queued_run(
    research_client,
    user_headers,
    user_id,
    run_enqueuer,
    deadline_scheduler,
    system_database,
):
    """新しいスレッドで質問すると、質問とqueuedのrunが保存され、実行が投入される。"""
    response = await research_client.post(
        "/api/v1/research/responses",
        json={"question": "半導体の需給はどうなる？"},
        headers=user_headers,
    )

    assert response.status_code == 202
    started = response.json()
    thread_id = UUID(started["threadId"])
    run_id = UUID(started["runId"])
    assert await thread_record(system_database, thread_id) == {
        "user_id": user_id,
        "title": "半導体の需給はどうなる？",
        "messages": [{"seq": 1, "role": "user", "content": "半導体の需給はどうなる？"}],
        "runs": [{"id": run_id, "status": "queued", "error_code": None}],
    }
    quota = await daily_quota_counts(system_database, user_id)
    assert list(quota.values()) == [1]
    assert run_enqueuer.enqueued == [run_id]
    assert [reserved for reserved, _ in deadline_scheduler.reserved] == [run_id]


async def test_asking_in_existing_thread_appends_question(
    research_client, user_headers, user_id, system_database
):
    """回答済みのスレッドで質問すると、同じスレッドに次の質問とrunが加わる。"""
    last_active_at = datetime(2026, 9, 20, 9, 1, tzinfo=UTC)
    thread_id = await seed_thread(
        system_database, user_id=user_id, title="前の質問", updated_at=last_active_at
    )
    previous = await seed_question(
        system_database,
        thread_id=thread_id,
        seq=1,
        content="前の質問",
        status="running",
        attempt_epoch=1,
    )
    await seed_answer(
        system_database,
        thread_id=thread_id,
        run_id=previous.run_id,
        seq=2,
        content="前の回答",
    )

    response = await research_client.post(
        "/api/v1/research/responses",
        json={"question": "次の質問", "threadId": str(thread_id)},
        headers=user_headers,
    )

    assert response.status_code == 202
    started = response.json()
    assert UUID(started["threadId"]) == thread_id
    assert await thread_record(system_database, thread_id) == {
        "user_id": user_id,
        "title": "前の質問",
        "messages": [
            {"seq": 1, "role": "user", "content": "前の質問"},
            {"seq": 2, "role": "assistant", "content": "前の回答"},
            {"seq": 3, "role": "user", "content": "次の質問"},
        ],
        "runs": [
            {"id": previous.run_id, "status": "completed", "error_code": None},
            {"id": UUID(started["runId"]), "status": "queued", "error_code": None},
        ],
    }
    assert await thread_updated_at(system_database, thread_id) > last_active_at


async def test_failed_enqueue_records_run_as_failed(
    research_client, user_headers, user_id, run_enqueuer, system_database
):
    """実行の投入に失敗すると、runが投入失敗として保存される。"""
    run_enqueuer.failure = RuntimeError("broker unavailable")

    response = await research_client.post(
        "/api/v1/research/responses",
        json={"question": "投入に失敗する質問"},
        headers=user_headers,
    )

    assert response.status_code == 202
    started = response.json()
    record = await thread_record(system_database, UUID(started["threadId"]))
    assert record["runs"] == [
        {
            "id": UUID(started["runId"]),
            "status": "failed",
            "error_code": "enqueue_failed",
        }
    ]


async def test_cancelling_queued_run_returns_quota(
    research_client, user_headers, user_id, system_database
):
    """queuedのrunを取り消すと取消として確定し、予約した利用枠が戻る。"""
    thread_id = await seed_thread(system_database, user_id=user_id, title="質問")
    question = await seed_question(
        system_database,
        thread_id=thread_id,
        seq=1,
        content="質問",
        status="queued",
        quota_usage_date=QUOTA_DATE,
    )
    await seed_daily_quota(
        system_database, user_id=user_id, usage_date=QUOTA_DATE, used_count=1
    )

    response = await research_client.post(
        f"/api/v1/research/runs/{question.run_id}/cancel", headers=user_headers
    )

    assert response.status_code == 204
    record = await thread_record(system_database, thread_id)
    assert record["runs"] == [
        {"id": question.run_id, "status": "failed", "error_code": "cancelled"}
    ]
    assert await daily_quota_counts(system_database, user_id) == {QUOTA_DATE: 0}


async def test_cancelling_running_run_publishes_terminal_event(
    research_client, user_headers, user_id, live_redis, system_database
):
    """runningのrunを取り消すと取消として確定し、終了を配信する。利用枠は戻らない。"""
    thread_id = await seed_thread(system_database, user_id=user_id, title="質問")
    question = await seed_question(
        system_database,
        thread_id=thread_id,
        seq=1,
        content="質問",
        status="running",
        attempt_epoch=1,
        quota_usage_date=QUOTA_DATE,
    )
    await seed_daily_quota(
        system_database, user_id=user_id, usage_date=QUOTA_DATE, used_count=1
    )

    response = await research_client.post(
        f"/api/v1/research/runs/{question.run_id}/cancel", headers=user_headers
    )

    assert response.status_code == 204
    record = await thread_record(system_database, thread_id)
    assert record["runs"] == [
        {"id": question.run_id, "status": "failed", "error_code": "cancelled"}
    ]
    assert live_redis.terminal_events(question.run_id) == [
        {"status": "failed", "errorCode": "cancelled"}
    ]
    assert await daily_quota_counts(system_database, user_id) == {QUOTA_DATE: 1}


async def test_thread_list_shows_active_run_newest_first(
    research_client, user_headers, user_id, system_database
):
    """スレッドの一覧は、実行中のrunの有無を付けて最近動いた順に返す。"""
    answered = await seed_thread(
        system_database,
        user_id=user_id,
        title="回答済みの質問",
        updated_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    question = await seed_question(
        system_database,
        thread_id=answered,
        seq=1,
        content="回答済みの質問",
        status="running",
        attempt_epoch=1,
    )
    await seed_answer(
        system_database,
        thread_id=answered,
        run_id=question.run_id,
        seq=2,
        content="回答",
    )
    pending = await seed_thread(
        system_database,
        user_id=user_id,
        title="実行中の質問",
        updated_at=datetime(2026, 9, 21, tzinfo=UTC),
    )
    await seed_question(
        system_database,
        thread_id=pending,
        seq=1,
        content="実行中の質問",
        status="queued",
    )

    response = await research_client.get(
        "/api/v1/research/threads", headers=user_headers
    )

    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "threadId": str(pending),
            "title": "実行中の質問",
            "updatedAt": "2026-09-21T00:00:00Z",
            "hasActiveRun": True,
        },
        {
            "threadId": str(answered),
            "title": "回答済みの質問",
            "updatedAt": "2026-09-20T00:00:00Z",
            "hasActiveRun": False,
        },
    ]


async def test_answered_thread_detail_returns_answer_with_sources(
    research_client, user_headers, user_id, system_database
):
    """回答済みのスレッドを開くと、質問・回答・出典がそろって返る。"""
    thread_id = await seed_thread(system_database, user_id=user_id, title="質問")
    question = await seed_question(
        system_database,
        thread_id=thread_id,
        seq=1,
        content="質問",
        status="running",
        attempt_epoch=1,
        created_at=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
    )
    answer_id = await seed_answer(
        system_database,
        thread_id=thread_id,
        run_id=question.run_id,
        seq=2,
        content="回答 [[1]]",
        created_at=datetime(2026, 9, 20, 9, 1, tzinfo=UTC),
    )
    await seed_external_source(
        system_database,
        message_id=answer_id,
        source_ref="1",
        url="https://example.com/evidence",
        title="根拠の記事",
        source_name="Example",
        published_at=datetime(2026, 9, 19, tzinfo=UTC),
        evidence_claim="根拠の引用",
    )

    response = await research_client.get(
        f"/api/v1/research/threads/{thread_id}", headers=user_headers
    )

    assert response.status_code == 200
    assert response.json() == {
        "threadId": str(thread_id),
        "title": "質問",
        "messages": [
            {
                "role": "user",
                "seq": 1,
                "content": "質問",
                "createdAt": "2026-09-20T09:00:00Z",
                "run": {
                    "runId": str(question.run_id),
                    "status": "completed",
                    "errorCode": None,
                },
            },
            {
                "role": "assistant",
                "seq": 2,
                "content": "回答 [[1]]",
                "createdAt": "2026-09-20T09:01:00Z",
                "sources": [
                    {
                        "kind": "external_url",
                        "sourceRef": "1",
                        "url": "https://example.com/evidence",
                        "title": "根拠の記事",
                        "sourceName": "Example",
                        "publishedAt": "2026-09-19T00:00:00Z",
                        "evidenceClaim": "根拠の引用",
                    }
                ],
                "missingAspects": [],
            },
        ],
    }


async def test_opening_thread_finalizes_expired_queued_run(
    research_client, user_headers, user_id, system_database
):
    """期限を過ぎたqueuedのrunがあるスレッドを開くと、期限切れに確定し利用枠が戻る。"""
    thread_id = await seed_thread(system_database, user_id=user_id, title="質問")
    question = await seed_question(
        system_database,
        thread_id=thread_id,
        seq=1,
        content="質問",
        status="queued",
        deadline_at=datetime(2026, 9, 20, 9, 1, tzinfo=UTC),
        quota_usage_date=QUOTA_DATE,
    )
    await seed_daily_quota(
        system_database, user_id=user_id, usage_date=QUOTA_DATE, used_count=1
    )

    response = await research_client.get(
        f"/api/v1/research/threads/{thread_id}", headers=user_headers
    )

    assert response.status_code == 200
    assert [message["run"] for message in response.json()["messages"]] == [
        {
            "runId": str(question.run_id),
            "status": "deadline_exceeded",
            "errorCode": None,
        }
    ]
    record = await thread_record(system_database, thread_id)
    assert record["runs"] == [
        {"id": question.run_id, "status": "deadline_exceeded", "error_code": None}
    ]
    assert await daily_quota_counts(system_database, user_id) == {QUOTA_DATE: 0}


async def test_run_returns_its_state(
    research_client, user_headers, user_id, system_database
):
    """runを求めると、その状態が返る。"""
    thread_id = await seed_thread(system_database, user_id=user_id, title="質問")
    question = await seed_question(
        system_database,
        thread_id=thread_id,
        seq=1,
        content="質問",
        status="queued",
    )

    response = await research_client.get(
        f"/api/v1/research/runs/{question.run_id}", headers=user_headers
    )

    assert response.status_code == 200
    assert response.json() == {
        "runId": str(question.run_id),
        "threadId": str(thread_id),
        "status": "queued",
        "errorCode": None,
        "attemptEpoch": 0,
    }


async def test_subscribing_to_finished_run_returns_no_content(
    research_client, user_headers, user_id, system_database
):
    """終了済みのrunの実行状況を購読すると、配信を始めずに204を返す。"""
    thread_id = await seed_thread(system_database, user_id=user_id, title="質問")
    question = await seed_question(
        system_database,
        thread_id=thread_id,
        seq=1,
        content="質問",
        status="failed",
        error_code="cancelled",
    )

    response = await research_client.get(
        f"/api/v1/research/runs/{question.run_id}/events", headers=user_headers
    )

    assert response.status_code == 204


async def test_deleting_thread_removes_its_history(
    research_client, user_headers, user_id, system_database
):
    """スレッドを削除すると、質問・回答・run・出典もまとめて消える。"""
    thread_id = await seed_thread(system_database, user_id=user_id, title="質問")
    question = await seed_question(
        system_database,
        thread_id=thread_id,
        seq=1,
        content="質問",
        status="running",
        attempt_epoch=1,
    )
    answer_id = await seed_answer(
        system_database,
        thread_id=thread_id,
        run_id=question.run_id,
        seq=2,
        content="回答 [[1]]",
    )
    await seed_external_source(
        system_database,
        message_id=answer_id,
        source_ref="1",
        url="https://example.com/evidence",
        title="根拠の記事",
        source_name="Example",
        published_at=datetime(2026, 9, 19, tzinfo=UTC),
        evidence_claim="根拠の引用",
    )

    response = await research_client.delete(
        f"/api/v1/research/threads/{thread_id}", headers=user_headers
    )

    assert response.status_code == 204
    assert await research_row_counts(system_database) == {
        "agent_threads": 0,
        "agent_messages": 0,
        "agent_runs": 0,
        "agent_message_sources": 0,
    }
