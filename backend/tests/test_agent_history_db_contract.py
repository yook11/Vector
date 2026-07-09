"""Agent 会話履歴 4 テーブルの DB 契約テスト。

正本仕様: ``specs/agent-history-schema-slice.md`` の Tests (1-18)。対象は
``app/models/agent_thread.py`` / ``agent_message.py`` / ``agent_run.py`` (model)
と ``alembic/versions/y1_agent_history.py`` (migration) が同一定義で焼く
unique / check / composite FK / partial unique index。model と migration は
別ファイルに同一制約を持つため、本テストは ``Base.metadata.create_all`` 経由
(conftest の ``setup_db``) で model 側の制約が実効することを検証する
(migration 側の contract は ``alembic upgrade head`` / ``downgrade -1`` の
ローカル往復で別途担保する)。

挿入は SQLAlchemy Core (``insert()``) で行う。ORM ``session.add`` の unit of
work バッファリングを避け、1 insert = 1 SQL 文で境界を壊した baseline を
そのまま DB に当てるため。IntegrityError は sqlstate + constraint_name で
確認する (``pytest.raises(IntegrityError)`` だけでは何の制約に落ちたか
特定できず、無関係な NOT NULL 違反等で偶然 green になる余地が残るため)。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import JSON, ForeignKeyConstraint, func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import app.models as _models  # noqa: F401  # populate Base.metadata
from app.models.base import Base
from app.models.category import Category
from app.models.news_source import NewsSource
from tests.conftest import TEST_USER_ID

AGENT_THREADS = Base.metadata.tables["agent_threads"]
AGENT_MESSAGES = Base.metadata.tables["agent_messages"]
AGENT_MESSAGE_SOURCES = Base.metadata.tables["agent_message_sources"]
AGENT_RUNS = Base.metadata.tables["agent_runs"]
ANALYZABLE_ARTICLES = Base.metadata.tables["analyzable_articles"]
ARTICLE_CURATIONS = Base.metadata.tables["article_curations"]
ANALYZED_ARTICLES = Base.metadata.tables["analyzed_articles"]

# PostgreSQL sqlstate (asyncpg 例外の ``sqlstate`` / ``pgcode``)。
CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"


# ---------------------------------------------------------------------------
# IntegrityError の検証ヘルパ
# ---------------------------------------------------------------------------


def _integrity_error_detail(exc: IntegrityError) -> tuple[str | None, str | None]:
    """asyncpg 例外から (sqlstate, constraint_name) を緩く取り出す。

    SQLAlchemy の asyncpg アダプタ例外 (``exc.orig``) は sqlstate は持つが
    constraint_name を落とすため、実体の asyncpg 例外 (``orig.__cause__``)
    側も見る。
    """
    orig = exc.orig
    cause = getattr(orig, "__cause__", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(cause, "sqlstate", None)
    constraint_name = getattr(orig, "constraint_name", None) or getattr(
        cause, "constraint_name", None
    )
    return sqlstate, constraint_name


async def _assert_integrity_violation(
    session: AsyncSession,
    stmt: object,
    *,
    sqlstate: str,
    constraint_name: str,
) -> None:
    """``stmt`` の実行が指定 sqlstate + constraint_name で拒否されることを確認する。"""
    with pytest.raises(IntegrityError) as exc_info:
        await session.execute(stmt)  # type: ignore[arg-type]
    actual_sqlstate, actual_constraint_name = _integrity_error_detail(exc_info.value)
    assert (actual_sqlstate, actual_constraint_name) == (sqlstate, constraint_name)
    await session.rollback()


# ---------------------------------------------------------------------------
# 正常系 insert ヘルパ (baseline)
# ---------------------------------------------------------------------------


async def _insert_thread(
    session: AsyncSession, *, user_id: str = TEST_USER_ID, title: str = "Thread"
) -> uuid.UUID:
    result = await session.execute(
        insert(AGENT_THREADS)
        .values(user_id=user_id, title=title)
        .returning(AGENT_THREADS.c.id)
    )
    return result.scalar_one()


async def _insert_message(
    session: AsyncSession,
    *,
    thread_id: uuid.UUID,
    seq: int,
    role: str,
    content: str,
    missing_aspects: object = None,
) -> uuid.UUID:
    values: dict[str, object] = {
        "thread_id": thread_id,
        "seq": seq,
        "role": role,
        "content": content,
    }
    if missing_aspects is not None:
        values["missing_aspects"] = missing_aspects
    result = await session.execute(
        insert(AGENT_MESSAGES).values(**values).returning(AGENT_MESSAGES.c.id)
    )
    return result.scalar_one()


async def _insert_run(
    session: AsyncSession,
    *,
    thread_id: uuid.UUID,
    user_message_id: uuid.UUID,
    assistant_message_id: uuid.UUID | None = None,
    status: str = "queued",
    progress_stage: str | None = None,
    error_code: str | None = None,
) -> uuid.UUID:
    result = await session.execute(
        insert(AGENT_RUNS)
        .values(
            thread_id=thread_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            status=status,
            progress_stage=progress_stage,
            error_code=error_code,
        )
        .returning(AGENT_RUNS.c.id)
    )
    return result.scalar_one()


def _valid_external_source_values(
    *, message_id: uuid.UUID, ordinal: int = 1, source_ref: str = "s1"
) -> dict[str, object]:
    return {
        "message_id": message_id,
        "ordinal": ordinal,
        "kind": "external_url",
        "source_ref": source_ref,
        "url": "https://example.com/article",
        "title": "External title",
        "evidence_claim": "Supports the claim.",
    }


def _valid_internal_source_values(
    *,
    message_id: uuid.UUID,
    analyzed_article_id: int | None,
    ordinal: int = 1,
    source_ref: str = "s1",
) -> dict[str, object]:
    return {
        "message_id": message_id,
        "ordinal": ordinal,
        "kind": "internal_article",
        "source_ref": source_ref,
        "analyzed_article_id": analyzed_article_id,
        "title": "Internal title",
    }


async def _insert_source(session: AsyncSession, values: dict[str, object]) -> int:
    result = await session.execute(
        insert(AGENT_MESSAGE_SOURCES)
        .values(**values)
        .returning(AGENT_MESSAGE_SOURCES.c.id)
    )
    return result.scalar_one()


async def _seed_analyzed_article(
    session: AsyncSession, *, source_id: int, category_id: int
) -> int:
    """internal source の FK 対象になる analyzed_article を最小チェーンで作る。

    chain: news_source (呼び出し側が用意) -> analyzable_article -> curation
    -> analyzed_article。url は呼び出しごとに一意にし
    ``uq_analyzable_articles_source_url`` の衝突を避ける。
    """
    unique_ref = uuid.uuid4().hex
    article_id = (
        await session.execute(
            insert(ANALYZABLE_ARTICLES)
            .values(
                source_id=source_id,
                source_url=f"https://example.com/agent-history-{unique_ref}",
                original_title="Original title",
                original_content="content",
                published_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            .returning(ANALYZABLE_ARTICLES.c.id)
        )
    ).scalar_one()
    curation_id = (
        await session.execute(
            insert(ARTICLE_CURATIONS)
            .values(
                analyzable_article_id=article_id,
                translated_title="Translated title",
                summary="Summary",
            )
            .returning(ARTICLE_CURATIONS.c.id)
        )
    ).scalar_one()
    analyzed_article_id = (
        await session.execute(
            insert(ANALYZED_ARTICLES)
            .values(
                curation_id=curation_id,
                translated_title="Translated title",
                summary="Summary",
                investor_take="Investor take",
                category_id=category_id,
            )
            .returning(ANALYZED_ARTICLES.c.id)
        )
    ).scalar_one()
    return analyzed_article_id


async def _seed_full_history(
    session: AsyncSession, *, user_id: str
) -> dict[str, uuid.UUID | int]:
    """thread + user/assistant message + external source + completed run の一式。"""
    thread_id = await _insert_thread(session, user_id=user_id)
    user_message_id = await _insert_message(
        session, thread_id=thread_id, seq=1, role="user", content="question"
    )
    assistant_message_id = await _insert_message(
        session, thread_id=thread_id, seq=2, role="assistant", content="answer"
    )
    source_id = await _insert_source(
        session, _valid_external_source_values(message_id=assistant_message_id)
    )
    run_id = await _insert_run(
        session,
        thread_id=thread_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        status="completed",
    )
    return {
        "thread_id": thread_id,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "source_id": source_id,
        "run_id": run_id,
    }


# ---------------------------------------------------------------------------
# Test 1: user 削除 → threads/messages/sources/runs cascade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_user_cascades_to_thread(db_session: AsyncSession) -> None:
    new_user_id = str(uuid.uuid4())
    await db_session.execute(
        text('INSERT INTO auth."user" (id) VALUES (:uid)'), {"uid": new_user_id}
    )
    seed = await _seed_full_history(db_session, user_id=new_user_id)
    await db_session.commit()

    await db_session.execute(
        text('DELETE FROM auth."user" WHERE id = :uid'), {"uid": new_user_id}
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(AGENT_THREADS).where(AGENT_THREADS.c.id == seed["thread_id"])
        )
    ).first()
    assert row is None


@pytest.mark.asyncio
async def test_deleting_user_cascades_to_messages(db_session: AsyncSession) -> None:
    new_user_id = str(uuid.uuid4())
    await db_session.execute(
        text('INSERT INTO auth."user" (id) VALUES (:uid)'), {"uid": new_user_id}
    )
    seed = await _seed_full_history(db_session, user_id=new_user_id)
    await db_session.commit()

    await db_session.execute(
        text('DELETE FROM auth."user" WHERE id = :uid'), {"uid": new_user_id}
    )
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(AGENT_MESSAGES).where(
                AGENT_MESSAGES.c.thread_id == seed["thread_id"]
            )
        )
    ).all()
    assert rows == []


@pytest.mark.asyncio
async def test_deleting_user_cascades_to_sources(db_session: AsyncSession) -> None:
    new_user_id = str(uuid.uuid4())
    await db_session.execute(
        text('INSERT INTO auth."user" (id) VALUES (:uid)'), {"uid": new_user_id}
    )
    seed = await _seed_full_history(db_session, user_id=new_user_id)
    await db_session.commit()

    await db_session.execute(
        text('DELETE FROM auth."user" WHERE id = :uid'), {"uid": new_user_id}
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(AGENT_MESSAGE_SOURCES).where(
                AGENT_MESSAGE_SOURCES.c.id == seed["source_id"]
            )
        )
    ).first()
    assert row is None


@pytest.mark.asyncio
async def test_deleting_user_cascades_to_runs(db_session: AsyncSession) -> None:
    new_user_id = str(uuid.uuid4())
    await db_session.execute(
        text('INSERT INTO auth."user" (id) VALUES (:uid)'), {"uid": new_user_id}
    )
    seed = await _seed_full_history(db_session, user_id=new_user_id)
    await db_session.commit()

    await db_session.execute(
        text('DELETE FROM auth."user" WHERE id = :uid'), {"uid": new_user_id}
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(AGENT_RUNS).where(AGENT_RUNS.c.id == seed["run_id"])
        )
    ).first()
    assert row is None


# ---------------------------------------------------------------------------
# Test 2: thread 削除 → messages/sources/runs cascade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_thread_cascades_to_messages(db_session: AsyncSession) -> None:
    seed = await _seed_full_history(db_session, user_id=TEST_USER_ID)
    await db_session.commit()

    await db_session.execute(
        AGENT_THREADS.delete().where(AGENT_THREADS.c.id == seed["thread_id"])
    )
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(AGENT_MESSAGES).where(
                AGENT_MESSAGES.c.thread_id == seed["thread_id"]
            )
        )
    ).all()
    assert rows == []


@pytest.mark.asyncio
async def test_deleting_thread_cascades_to_sources(db_session: AsyncSession) -> None:
    seed = await _seed_full_history(db_session, user_id=TEST_USER_ID)
    await db_session.commit()

    await db_session.execute(
        AGENT_THREADS.delete().where(AGENT_THREADS.c.id == seed["thread_id"])
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(AGENT_MESSAGE_SOURCES).where(
                AGENT_MESSAGE_SOURCES.c.id == seed["source_id"]
            )
        )
    ).first()
    assert row is None


@pytest.mark.asyncio
async def test_deleting_thread_cascades_to_runs(db_session: AsyncSession) -> None:
    seed = await _seed_full_history(db_session, user_id=TEST_USER_ID)
    await db_session.commit()

    await db_session.execute(
        AGENT_THREADS.delete().where(AGENT_THREADS.c.id == seed["thread_id"])
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(AGENT_RUNS).where(AGENT_RUNS.c.id == seed["run_id"])
        )
    ).first()
    assert row is None


# ---------------------------------------------------------------------------
# Test 3: analyzed_article 削除 → source は残り analyzed_article_id が NULL、
# snapshot (title 等) は保持される (SET NULL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_analyzed_article_set_nulls_source_fk_and_keeps_snapshot(
    db_session: AsyncSession,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> None:
    thread_id = await _insert_thread(db_session)
    assistant_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="assistant", content="answer"
    )
    analyzed_article_id = await _seed_analyzed_article(
        db_session, source_id=sample_source.id, category_id=sample_categories[0].id
    )
    values = _valid_internal_source_values(
        message_id=assistant_message_id, analyzed_article_id=analyzed_article_id
    )
    values["title"] = "Snapshot Title Preserved"
    source_id = await _insert_source(db_session, values)
    await db_session.commit()

    await db_session.execute(
        ANALYZED_ARTICLES.delete().where(ANALYZED_ARTICLES.c.id == analyzed_article_id)
    )
    await db_session.commit()

    row = (
        await db_session.execute(
            select(AGENT_MESSAGE_SOURCES).where(AGENT_MESSAGE_SOURCES.c.id == source_id)
        )
    ).first()
    assert row is not None
    assert row.analyzed_article_id is None
    assert row.title == "Snapshot Title Preserved"


# ---------------------------------------------------------------------------
# Test 4: uq_agent_runs_thread_active
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_active_run_in_thread_violates_thread_active_unique(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    user_message_1 = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="user", content="q1"
    )
    user_message_2 = await _insert_message(
        db_session, thread_id=thread_id, seq=2, role="user", content="q2"
    )
    await _insert_run(
        db_session, thread_id=thread_id, user_message_id=user_message_1, status="queued"
    )

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_RUNS).values(
            thread_id=thread_id, user_message_id=user_message_2, status="running"
        ),
        sqlstate=UNIQUE_VIOLATION,
        constraint_name="uq_agent_runs_thread_active",
    )


@pytest.mark.asyncio
async def test_queued_run_allowed_when_thread_only_has_completed_run(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    user_message_1 = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="user", content="q1"
    )
    assistant_message_1 = await _insert_message(
        db_session, thread_id=thread_id, seq=2, role="assistant", content="a1"
    )
    user_message_2 = await _insert_message(
        db_session, thread_id=thread_id, seq=3, role="user", content="q2"
    )
    await _insert_run(
        db_session,
        thread_id=thread_id,
        user_message_id=user_message_1,
        assistant_message_id=assistant_message_1,
        status="completed",
    )
    await db_session.commit()

    await _insert_run(
        db_session, thread_id=thread_id, user_message_id=user_message_2, status="queued"
    )
    await db_session.commit()

    run_count = await db_session.scalar(
        select(func.count())
        .select_from(AGENT_RUNS)
        .where(AGENT_RUNS.c.thread_id == thread_id)
    )
    assert run_count == 2


# ---------------------------------------------------------------------------
# Test 5: uq_agent_runs_user_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_run_reusing_user_message_violates_user_message_unique(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    user_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="user", content="q1"
    )
    await _insert_run(
        db_session,
        thread_id=thread_id,
        user_message_id=user_message_id,
        status="failed",
        error_code="boom",
    )

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_RUNS).values(
            thread_id=thread_id, user_message_id=user_message_id, status="queued"
        ),
        sqlstate=UNIQUE_VIOLATION,
        constraint_name="uq_agent_runs_user_message",
    )


# ---------------------------------------------------------------------------
# Test 6: ck_agent_runs_completed_answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_run_without_assistant_message_violates_completed_answer_check(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    user_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="user", content="q1"
    )

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_RUNS).values(
            thread_id=thread_id, user_message_id=user_message_id, status="completed"
        ),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_runs_completed_answer",
    )


@pytest.mark.asyncio
async def test_running_run_with_assistant_message_violates_completed_answer_check(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    user_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="user", content="q1"
    )
    assistant_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=2, role="assistant", content="a1"
    )

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_RUNS).values(
            thread_id=thread_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            status="running",
        ),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_runs_completed_answer",
    )


# ---------------------------------------------------------------------------
# Test 7: uq_agent_runs_assistant_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_run_reusing_assistant_message_violates_assistant_message_unique(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    user_message_1 = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="user", content="q1"
    )
    assistant_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=2, role="assistant", content="a1"
    )
    user_message_2 = await _insert_message(
        db_session, thread_id=thread_id, seq=3, role="user", content="q2"
    )
    await _insert_run(
        db_session,
        thread_id=thread_id,
        user_message_id=user_message_1,
        assistant_message_id=assistant_message_id,
        status="completed",
    )

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_RUNS).values(
            thread_id=thread_id,
            user_message_id=user_message_2,
            assistant_message_id=assistant_message_id,
            status="completed",
        ),
        sqlstate=UNIQUE_VIOLATION,
        constraint_name="uq_agent_runs_assistant_message",
    )


# ---------------------------------------------------------------------------
# Test 8: ck_agent_runs_failed_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_run_without_error_code_violates_failed_error_check(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    user_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="user", content="q1"
    )

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_RUNS).values(
            thread_id=thread_id, user_message_id=user_message_id, status="failed"
        ),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_runs_failed_error",
    )


@pytest.mark.asyncio
async def test_completed_run_with_error_code_violates_failed_error_check(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    user_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="user", content="q1"
    )
    assistant_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=2, role="assistant", content="a1"
    )

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_RUNS).values(
            thread_id=thread_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            status="completed",
            error_code="boom",
        ),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_runs_failed_error",
    )


# ---------------------------------------------------------------------------
# Test 9: composite FK (run と message の同一 thread 整合)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_user_message_from_different_thread_violates_composite_fk(
    db_session: AsyncSession,
) -> None:
    thread_a = await _insert_thread(db_session)
    thread_b = await _insert_thread(db_session)
    other_thread_message_id = await _insert_message(
        db_session, thread_id=thread_b, seq=1, role="user", content="q"
    )

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_RUNS).values(
            thread_id=thread_a, user_message_id=other_thread_message_id, status="queued"
        ),
        sqlstate=FOREIGN_KEY_VIOLATION,
        constraint_name="fk_agent_runs_thread_user_message",
    )


@pytest.mark.asyncio
async def test_run_assistant_message_from_different_thread_violates_composite_fk(
    db_session: AsyncSession,
) -> None:
    thread_a = await _insert_thread(db_session)
    thread_b = await _insert_thread(db_session)
    user_message_id = await _insert_message(
        db_session, thread_id=thread_a, seq=1, role="user", content="q"
    )
    other_thread_assistant_message_id = await _insert_message(
        db_session, thread_id=thread_b, seq=1, role="assistant", content="a"
    )

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_RUNS).values(
            thread_id=thread_a,
            user_message_id=user_message_id,
            assistant_message_id=other_thread_assistant_message_id,
            status="completed",
        ),
        sqlstate=FOREIGN_KEY_VIOLATION,
        constraint_name="fk_agent_runs_thread_assistant_message",
    )


@pytest.mark.asyncio
async def test_run_with_same_thread_messages_is_allowed(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    user_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="user", content="q"
    )
    assistant_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=2, role="assistant", content="a"
    )

    run_id = await _insert_run(
        db_session,
        thread_id=thread_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        status="completed",
    )
    await db_session.commit()

    row = (
        await db_session.execute(select(AGENT_RUNS).where(AGENT_RUNS.c.id == run_id))
    ).first()
    assert row is not None


# ---------------------------------------------------------------------------
# Test 10: uq_agent_messages_thread_seq
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_thread_seq_violates_thread_seq_unique(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="user", content="q1"
    )

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_MESSAGES).values(
            thread_id=thread_id, seq=1, role="user", content="q2"
        ),
        sqlstate=UNIQUE_VIOLATION,
        constraint_name="uq_agent_messages_thread_seq",
    )


# ---------------------------------------------------------------------------
# Test 11: ck_agent_messages_missing_aspects_role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_message_with_missing_aspects_violates_missing_aspects_role_check(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_MESSAGES).values(
            thread_id=thread_id,
            seq=1,
            role="user",
            content="q1",
            missing_aspects=["unexpected gap"],
        ),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_messages_missing_aspects_role",
    )


# ---------------------------------------------------------------------------
# Test 12: ck_agent_messages_missing_aspects_array
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_aspects",
    [{"gap": "network"}, "just a string", JSON.NULL],
    ids=["object", "string", "json_null"],
)
async def test_assistant_message_with_non_array_missing_aspects_violates_array_check(
    db_session: AsyncSession, missing_aspects: object
) -> None:
    thread_id = await _insert_thread(db_session)

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_MESSAGES).values(
            thread_id=thread_id,
            seq=1,
            role="assistant",
            content="a1",
            missing_aspects=missing_aspects,
        ),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_messages_missing_aspects_array",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_aspects",
    [[], ["missing revenue breakdown"]],
    ids=["empty_array", "with_values"],
)
async def test_assistant_message_with_array_missing_aspects_is_allowed(
    db_session: AsyncSession, missing_aspects: list[str]
) -> None:
    thread_id = await _insert_thread(db_session)

    message_id = await _insert_message(
        db_session,
        thread_id=thread_id,
        seq=1,
        role="assistant",
        content="a1",
        missing_aspects=missing_aspects,
    )
    await db_session.commit()

    stored = await db_session.scalar(
        select(AGENT_MESSAGES.c.missing_aspects).where(
            AGENT_MESSAGES.c.id == message_id
        )
    )
    assert stored == missing_aspects


# ---------------------------------------------------------------------------
# Test 13: ck_agent_message_sources_external_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", None),
        ("url", "ftp://example.com/malicious"),
        ("url", "https://example.com/" + "a" * 2040),
        ("evidence_claim", None),
        ("evidence_claim", ""),
    ],
    ids=[
        "url_null",
        "url_non_http_scheme",
        "url_over_2048_chars",
        "evidence_claim_null",
        "evidence_claim_empty",
    ],
)
async def test_external_source_violates_external_url_check(
    db_session: AsyncSession, field: str, value: object
) -> None:
    thread_id = await _insert_thread(db_session)
    assistant_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="assistant", content="a"
    )
    values = _valid_external_source_values(message_id=assistant_message_id)
    values[field] = value

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_MESSAGE_SOURCES).values(**values),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_message_sources_external_url",
    )


@pytest.mark.asyncio
async def test_external_source_with_analyzed_article_id_violates_external_url_check(
    db_session: AsyncSession,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> None:
    thread_id = await _insert_thread(db_session)
    assistant_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="assistant", content="a"
    )
    analyzed_article_id = await _seed_analyzed_article(
        db_session, source_id=sample_source.id, category_id=sample_categories[0].id
    )
    values = _valid_external_source_values(message_id=assistant_message_id)
    values["analyzed_article_id"] = analyzed_article_id

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_MESSAGE_SOURCES).values(**values),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_message_sources_external_url",
    )


@pytest.mark.asyncio
async def test_external_source_valid_row_is_allowed(db_session: AsyncSession) -> None:
    thread_id = await _insert_thread(db_session)
    assistant_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="assistant", content="a"
    )
    values = _valid_external_source_values(message_id=assistant_message_id)

    source_id = await _insert_source(db_session, values)
    await db_session.commit()

    row = (
        await db_session.execute(
            select(AGENT_MESSAGE_SOURCES).where(AGENT_MESSAGE_SOURCES.c.id == source_id)
        )
    ).first()
    assert row is not None


@pytest.mark.asyncio
async def test_external_source_uppercase_https_scheme_is_allowed(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    assistant_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="assistant", content="a"
    )
    values = _valid_external_source_values(message_id=assistant_message_id)
    values["url"] = "HTTPS://EXAMPLE.COM/ARTICLE"

    source_id = await _insert_source(db_session, values)
    await db_session.commit()

    stored_url = await db_session.scalar(
        select(AGENT_MESSAGE_SOURCES.c.url).where(
            AGENT_MESSAGE_SOURCES.c.id == source_id
        )
    )
    assert stored_url == "HTTPS://EXAMPLE.COM/ARTICLE"


# ---------------------------------------------------------------------------
# Test 14: ck_agent_message_sources_internal_article
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "https://example.com/leaked"),
        ("source_name", "Leaked Source"),
        ("evidence_claim", "Leaked claim"),
    ],
    ids=["url_set", "source_name_set", "evidence_claim_set"],
)
async def test_internal_source_violates_internal_article_check(
    db_session: AsyncSession,
    sample_categories: list[Category],
    sample_source: NewsSource,
    field: str,
    value: str,
) -> None:
    thread_id = await _insert_thread(db_session)
    assistant_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="assistant", content="a"
    )
    analyzed_article_id = await _seed_analyzed_article(
        db_session, source_id=sample_source.id, category_id=sample_categories[0].id
    )
    values = _valid_internal_source_values(
        message_id=assistant_message_id, analyzed_article_id=analyzed_article_id
    )
    values[field] = value

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_MESSAGE_SOURCES).values(**values),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_message_sources_internal_article",
    )


@pytest.mark.asyncio
async def test_internal_source_with_null_analyzed_article_id_is_allowed(
    db_session: AsyncSession,
) -> None:
    """記事削除後の正当状態 (SET NULL 後) を新規 insert でも再現できる。"""
    thread_id = await _insert_thread(db_session)
    assistant_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="assistant", content="a"
    )
    values = _valid_internal_source_values(
        message_id=assistant_message_id, analyzed_article_id=None
    )

    source_id = await _insert_source(db_session, values)
    await db_session.commit()

    row = (
        await db_session.execute(
            select(AGENT_MESSAGE_SOURCES).where(AGENT_MESSAGE_SOURCES.c.id == source_id)
        )
    ).first()
    assert row is not None


@pytest.mark.asyncio
async def test_internal_source_valid_row_is_allowed(
    db_session: AsyncSession,
    sample_categories: list[Category],
    sample_source: NewsSource,
) -> None:
    thread_id = await _insert_thread(db_session)
    assistant_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="assistant", content="a"
    )
    analyzed_article_id = await _seed_analyzed_article(
        db_session, source_id=sample_source.id, category_id=sample_categories[0].id
    )
    values = _valid_internal_source_values(
        message_id=assistant_message_id, analyzed_article_id=analyzed_article_id
    )

    source_id = await _insert_source(db_session, values)
    await db_session.commit()

    row = (
        await db_session.execute(
            select(AGENT_MESSAGE_SOURCES).where(AGENT_MESSAGE_SOURCES.c.id == source_id)
        )
    ).first()
    assert row is not None


# ---------------------------------------------------------------------------
# Test 15: 非空 CHECK 境界 (title / content / source_ref)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_with_empty_title_violates_title_not_empty_check(
    db_session: AsyncSession,
) -> None:
    await _assert_integrity_violation(
        db_session,
        insert(AGENT_THREADS).values(user_id=TEST_USER_ID, title=""),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_threads_title_not_empty",
    )


@pytest.mark.asyncio
async def test_message_with_empty_content_violates_content_not_empty_check(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_MESSAGES).values(
            thread_id=thread_id, seq=1, role="user", content=""
        ),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_messages_content_not_empty",
    )


@pytest.mark.asyncio
async def test_source_with_empty_source_ref_violates_source_ref_not_empty_check(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    assistant_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="assistant", content="a"
    )
    values = _valid_external_source_values(message_id=assistant_message_id)
    values["source_ref"] = ""

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_MESSAGE_SOURCES).values(**values),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_message_sources_source_ref_not_empty",
    )


@pytest.mark.asyncio
async def test_source_with_empty_title_violates_title_not_empty_check(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    assistant_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="assistant", content="a"
    )
    values = _valid_external_source_values(message_id=assistant_message_id)
    values["title"] = ""

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_MESSAGE_SOURCES).values(**values),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_message_sources_title_not_empty",
    )


# ---------------------------------------------------------------------------
# Test 16: check 系範囲外 (role / status / progress_stage / kind)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_with_invalid_role_violates_role_check(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_MESSAGES).values(
            thread_id=thread_id, seq=1, role="moderator", content="q"
        ),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_messages_role",
    )


@pytest.mark.asyncio
async def test_run_with_invalid_status_violates_status_check(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    user_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="user", content="q"
    )

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_RUNS).values(
            thread_id=thread_id, user_message_id=user_message_id, status="cancelled"
        ),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_runs_status",
    )


@pytest.mark.asyncio
async def test_run_with_invalid_progress_stage_violates_progress_stage_check(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    user_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="user", content="q"
    )

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_RUNS).values(
            thread_id=thread_id,
            user_message_id=user_message_id,
            status="running",
            progress_stage="drafting",
        ),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_runs_progress_stage",
    )


@pytest.mark.asyncio
async def test_source_with_invalid_kind_violates_kind_check(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    assistant_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="assistant", content="a"
    )

    await _assert_integrity_violation(
        db_session,
        insert(AGENT_MESSAGE_SOURCES).values(
            message_id=assistant_message_id,
            ordinal=1,
            kind="footnote",
            source_ref="s1",
            title="t",
        ),
        sqlstate=CHECK_VIOLATION,
        constraint_name="ck_agent_message_sources_kind",
    )


# ---------------------------------------------------------------------------
# Test 17: uuid pk の server_default 実効確認
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_id_is_generated_by_server_default(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    assert isinstance(thread_id, uuid.UUID)


@pytest.mark.asyncio
async def test_message_id_is_generated_by_server_default(
    db_session: AsyncSession,
) -> None:
    thread_id = await _insert_thread(db_session)
    message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="user", content="q"
    )
    assert isinstance(message_id, uuid.UUID)


@pytest.mark.asyncio
async def test_run_id_is_generated_by_server_default(db_session: AsyncSession) -> None:
    thread_id = await _insert_thread(db_session)
    user_message_id = await _insert_message(
        db_session, thread_id=thread_id, seq=1, role="user", content="q"
    )
    run_id = await _insert_run(
        db_session,
        thread_id=thread_id,
        user_message_id=user_message_id,
        status="queued",
    )
    assert isinstance(run_id, uuid.UUID)


# ---------------------------------------------------------------------------
# Test 18: parity (introspection) — FK target / ondelete / partial unique index
# ---------------------------------------------------------------------------


def _fk_constraint(table_name: str, constraint_name: str) -> ForeignKeyConstraint:
    """名前付き FK 制約を取り出す。

    ``column.foreign_keys`` は列単位の集約のため、``agent_runs.thread_id``
    のように 1 列が複数制約 (単純 FK + composite FK 2 本) に属す場合に他制約の
    ForeignKey まで拾ってしまう。制約名で引くことでこれを避ける。
    """
    for fk in Base.metadata.tables[table_name].foreign_key_constraints:
        if fk.name == constraint_name:
            return fk
    raise KeyError(constraint_name)


def _fk_targets(fk: ForeignKeyConstraint) -> list[str]:
    return [element.target_fullname for element in fk.elements]


def _index_where(table_name: str, index_name: str) -> str | None:
    for index in Base.metadata.tables[table_name].indexes:
        if index.name == index_name:
            where = index.dialect_options["postgresql"].get("where")
            return str(where) if where is not None else None
    raise KeyError(index_name)


def test_agent_threads_user_id_fk_cascades() -> None:
    fk = _fk_constraint("agent_threads", "fk_agent_threads_user_id")
    assert _fk_targets(fk) == ["auth.user.id"]
    assert fk.ondelete == "CASCADE"


def test_agent_messages_thread_id_fk_cascades() -> None:
    fk = _fk_constraint("agent_messages", "fk_agent_messages_thread_id")
    assert _fk_targets(fk) == ["agent_threads.id"]
    assert fk.ondelete == "CASCADE"


def test_agent_message_sources_message_id_fk_cascades() -> None:
    fk = _fk_constraint("agent_message_sources", "fk_agent_message_sources_message_id")
    assert _fk_targets(fk) == ["agent_messages.id"]
    assert fk.ondelete == "CASCADE"


def test_agent_message_sources_analyzed_article_id_fk_sets_null() -> None:
    fk = _fk_constraint(
        "agent_message_sources", "fk_agent_message_sources_analyzed_article_id"
    )
    assert _fk_targets(fk) == ["analyzed_articles.id"]
    assert fk.ondelete == "SET NULL"


def test_agent_runs_thread_id_fk_cascades() -> None:
    fk = _fk_constraint("agent_runs", "fk_agent_runs_thread_id")
    assert _fk_targets(fk) == ["agent_threads.id"]
    assert fk.ondelete == "CASCADE"


def test_agent_runs_composite_fk_user_message_targets_thread_and_message() -> None:
    fk = _fk_constraint("agent_runs", "fk_agent_runs_thread_user_message")
    assert fk.column_keys == ["thread_id", "user_message_id"]
    assert _fk_targets(fk) == ["agent_messages.thread_id", "agent_messages.id"]
    assert fk.ondelete == "CASCADE"


def test_agent_runs_composite_fk_assistant_message_targets_thread_and_message() -> None:
    fk = _fk_constraint("agent_runs", "fk_agent_runs_thread_assistant_message")
    assert fk.column_keys == ["thread_id", "assistant_message_id"]
    assert _fk_targets(fk) == ["agent_messages.thread_id", "agent_messages.id"]
    assert fk.ondelete == "CASCADE"


def test_agent_runs_thread_active_partial_unique_index_predicate() -> None:
    index = next(
        idx
        for idx in Base.metadata.tables["agent_runs"].indexes
        if idx.name == "uq_agent_runs_thread_active"
    )
    assert index.unique is True
    assert (
        _index_where("agent_runs", "uq_agent_runs_thread_active")
        == "status IN ('queued', 'running')"
    )
