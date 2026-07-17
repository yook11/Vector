"""Research pending navigation E2E用の固定threadを投入・削除する。"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, insert, or_, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncConnection  # noqa: E402

from app.config import settings  # noqa: E402
from app.db_ssl import create_app_engine  # noqa: E402
from app.models.agent_message import AgentMessage, AgentMessageSource  # noqa: E402
from app.models.agent_run import AgentRun  # noqa: E402
from app.models.agent_thread import AgentThread  # noqa: E402
from app.models.auth_ref import auth_user_ref  # noqa: E402

_E2E_USER_ID = uuid.UUID("01900000-0000-7000-a000-00000000e2e1")
_ALPHA_QUESTION = (
    "Alpha market question: 生成AI向け半導体、電力制約、データセンター投資、"
    "主要クラウド事業者の設備投資計画を横断し、需要の持続性と供給網のボトルネックを"
    "投資家向けに比較してください。短期的な受注の強さだけでなく、設備の稼働率、"
    "電力調達、先端パッケージ、HBM供給、顧客集中、規制リスクが中期の利益率へ与える"
    "影響も分け、確認可能な根拠と未確認事項を明示してください。"
)
_ALPHA_ANSWER = "\n\n".join(
    (
        "Alpha answer marker",
        *(
            f"分析セクション {index}: 需要、供給能力、電力、資本効率を分けて"
            "検証すると、"
            "足元の成長率だけでは持続性を判断できません。クラウド各社の設備投資、"
            "先端パッケージとHBMの供給制約、データセンターの系統接続時期を同じ時間軸で"
            f"比較する必要があります。根拠は外部ソース S{index} を参照します。"
            for index in range(1, 19)
        ),
    )
)
_ALPHA_MISSING_ASPECTS = (
    "地域別の系統接続待ち期間と電力価格の長期契約条件は公開情報だけでは比較できない",
    "顧客別の先端パッケージ予約量と解約条項は未開示で確度を評価できない",
    "次世代HBMの歩留まり改善時期は各社の説明に幅があり追加確認が必要",
)
_ALPHA_SOURCE_COUNT = 14


@dataclass(frozen=True)
class FixtureThread:
    label: str
    thread_id: uuid.UUID
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID
    run_id: uuid.UUID
    title: str
    question: str
    answer: str
    updated_at: dt.datetime
    missing_aspects: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContinuitySource:
    id: int
    ordinal: int
    source_ref: str
    url: str
    title: str
    source_name: str
    evidence_claim: str


@dataclass(frozen=True)
class ContinuityFixture:
    variant: str
    thread_id: uuid.UUID
    completed_user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID
    completed_run_id: uuid.UUID
    active_user_message_id: uuid.UUID
    active_run_id: uuid.UUID
    title: str
    completed_question: str
    answer: str
    active_question: str
    updated_at: dt.datetime
    missing_aspects: tuple[str, ...]
    sources: tuple[ContinuitySource, ...]


FIXTURE_THREADS = (
    FixtureThread(
        label="A",
        thread_id=uuid.UUID("00000000-0000-4000-a000-00000000e2a1"),
        user_message_id=uuid.UUID("00000000-0000-4000-a000-00000000a101"),
        assistant_message_id=uuid.UUID("00000000-0000-4000-a000-00000000a1a1"),
        run_id=uuid.UUID("00000000-0000-4000-a000-00000000a1f1"),
        title="E2E Research Alpha",
        question=_ALPHA_QUESTION,
        answer=_ALPHA_ANSWER,
        updated_at=dt.datetime(2026, 7, 11, 3, 0, tzinfo=dt.UTC),
        missing_aspects=_ALPHA_MISSING_ASPECTS,
    ),
    FixtureThread(
        label="B",
        thread_id=uuid.UUID("00000000-0000-4000-a000-00000000e2b2"),
        user_message_id=uuid.UUID("00000000-0000-4000-a000-00000000b201"),
        assistant_message_id=uuid.UUID("00000000-0000-4000-a000-00000000b2a1"),
        run_id=uuid.UUID("00000000-0000-4000-a000-00000000b2f1"),
        title="E2E Research Beta",
        question="Beta market question",
        answer="Beta answer marker",
        updated_at=dt.datetime(2026, 7, 11, 2, 0, tzinfo=dt.UTC),
    ),
    FixtureThread(
        label="C",
        thread_id=uuid.UUID("00000000-0000-4000-a000-00000000e2c3"),
        user_message_id=uuid.UUID("00000000-0000-4000-a000-00000000c301"),
        assistant_message_id=uuid.UUID("00000000-0000-4000-a000-00000000c3a1"),
        run_id=uuid.UUID("00000000-0000-4000-a000-00000000c3f1"),
        title="E2E Research Gamma",
        question="Gamma market question",
        answer="Gamma answer marker",
        updated_at=dt.datetime(2026, 7, 11, 1, 0, tzinfo=dt.UTC),
    ),
    *(
        FixtureThread(
            label=f"HISTORY_{index:02d}",
            thread_id=uuid.UUID(f"00000000-0000-4000-a100-{index:012x}"),
            user_message_id=uuid.UUID(f"00000000-0000-4000-a200-{index:012x}"),
            assistant_message_id=uuid.UUID(f"00000000-0000-4000-a300-{index:012x}"),
            run_id=uuid.UUID(f"00000000-0000-4000-a400-{index:012x}"),
            title=(
                f"E2E History {index:02d} — 長い履歴タイトルでも横方向へ"
                "はみ出さず省略表示されることを確認する固定スレッド"
            ),
            question=f"History question {index:02d}",
            answer=f"History answer {index:02d}",
            updated_at=dt.datetime(2026, 7, 10, 23, 0, tzinfo=dt.UTC)
            - dt.timedelta(minutes=index),
        )
        for index in range(1, 18)
    ),
)


def _continuity_answer(variant: str) -> str:
    return "\n\n".join(
        (
            f"Continuity {variant} persisted answer marker",
            *(
                f"継続回答セクション {index}: 過去回答と新しい質問の境界を保ったまま、"
                "市場データ、供給制約、投資計画を同じ時間軸で比較します。"
                "この固定本文はterminal refresh前後の回答領域とスクロール位置を"
                f"決定的に検証するための記録です。根拠 {index} を参照します。"
                for index in range(1, 15)
            ),
        )
    )


def _continuity_sources(
    variant: str,
    *,
    id_base: int,
) -> tuple[ContinuitySource, ...]:
    prefix = variant.upper()[0]
    return tuple(
        ContinuitySource(
            id=id_base + ordinal,
            ordinal=ordinal,
            source_ref=f"{prefix}{ordinal}",
            url=(
                f"https://example.com/e2e/research-continuity-{variant}/"
                f"source-{ordinal}"
            ),
            title=(
                f"Continuity {variant} source {ordinal:02d}: terminal refresh後も"
                "ソース面の状態とスクロール位置を維持する固定資料"
            ),
            source_name=f"Vector E2E Continuity {variant.title()} Monitor",
            evidence_claim=(
                "過去回答の根拠として保持される固定引用。"
                f"continuity {variant} のソース表示確認用資料 {ordinal} です。"
            ),
        )
        for ordinal in range(1, 15)
    )


CONTINUITY_FIXTURES = {
    "closed": ContinuityFixture(
        variant="closed",
        thread_id=uuid.UUID("00000000-0000-4000-a000-00000000e2d4"),
        completed_user_message_id=uuid.UUID("00000000-0000-4000-a000-00000000d401"),
        assistant_message_id=uuid.UUID("00000000-0000-4000-a000-00000000d4a1"),
        completed_run_id=uuid.UUID("00000000-0000-4000-a000-00000000d4f1"),
        active_user_message_id=uuid.UUID("00000000-0000-4000-a000-00000000d402"),
        active_run_id=uuid.UUID("00000000-0000-4000-a000-00000000d4f2"),
        title="E2E Research Continuity Closed",
        completed_question=(
            "Continuity closed completed question: "
            "過去の市場分析と根拠を整理してください。"
        ),
        answer=_continuity_answer("closed"),
        active_question=(
            "Continuity closed active question: "
            "同じスレッドで最新動向を追補してください。"
        ),
        updated_at=dt.datetime(2026, 7, 11, 1, 30, tzinfo=dt.UTC),
        missing_aspects=(
            "非公開の顧客別契約条件は確認できない",
            "地域別の電力調達時期は追加確認が必要",
        ),
        sources=_continuity_sources("closed", id_base=9_100_000_000_000),
    ),
    "open": ContinuityFixture(
        variant="open",
        thread_id=uuid.UUID("00000000-0000-4000-a000-00000000e2e5"),
        completed_user_message_id=uuid.UUID("00000000-0000-4000-a000-00000000e501"),
        assistant_message_id=uuid.UUID("00000000-0000-4000-a000-00000000e5a1"),
        completed_run_id=uuid.UUID("00000000-0000-4000-a000-00000000e5f1"),
        active_user_message_id=uuid.UUID("00000000-0000-4000-a000-00000000e502"),
        active_run_id=uuid.UUID("00000000-0000-4000-a000-00000000e5f2"),
        title="E2E Research Continuity Open",
        completed_question=(
            "Continuity open completed question: "
            "過去の供給網分析と根拠を整理してください。"
        ),
        answer=_continuity_answer("open"),
        active_question=(
            "Continuity open active question: "
            "同じスレッドで最新動向を追補してください。"
        ),
        updated_at=dt.datetime(2026, 7, 11, 1, 15, tzinfo=dt.UTC),
        missing_aspects=(
            "非公開の供給予約量は確認できない",
            "次世代製品の量産時期は追加確認が必要",
        ),
        sources=_continuity_sources("open", id_base=9_200_000_000_000),
    ),
}

_THREAD_IDS = (
    *(thread.thread_id for thread in FIXTURE_THREADS),
    *(fixture.thread_id for fixture in CONTINUITY_FIXTURES.values()),
)


def guard_production(environment: str) -> None:
    if environment.lower() != "production":
        return
    print(
        "ERROR: seed_e2e_research.py must NOT run in production.",
        file=sys.stderr,
    )
    raise SystemExit(2)


async def _cleanup(connection: AsyncConnection) -> None:
    await connection.execute(delete(AgentThread).where(AgentThread.id.in_(_THREAD_IDS)))


async def _seed(connection: AsyncConnection) -> None:
    owner = (
        await connection.execute(
            select(auth_user_ref.c.id).where(auth_user_ref.c.id == _E2E_USER_ID)
        )
    ).scalar_one_or_none()
    if owner is None:
        raise RuntimeError("E2E user is missing; run scripts/seed_e2e_users.py first")

    await _cleanup(connection)
    await connection.execute(
        insert(AgentThread),
        [
            {
                "id": thread.thread_id,
                "user_id": _E2E_USER_ID,
                "title": thread.title,
                "created_at": thread.updated_at,
                "updated_at": thread.updated_at,
            }
            for thread in FIXTURE_THREADS
        ],
    )
    await connection.execute(
        insert(AgentThread),
        [
            {
                "id": fixture.thread_id,
                "user_id": _E2E_USER_ID,
                "title": fixture.title,
                "created_at": fixture.updated_at,
                "updated_at": fixture.updated_at,
            }
            for fixture in CONTINUITY_FIXTURES.values()
        ],
    )
    await connection.execute(
        insert(AgentMessage),
        [
            row
            for thread in FIXTURE_THREADS
            for row in (
                {
                    "id": thread.user_message_id,
                    "thread_id": thread.thread_id,
                    "seq": 1,
                    "role": "user",
                    "content": thread.question,
                    "missing_aspects": [],
                    "created_at": thread.updated_at,
                },
                {
                    "id": thread.assistant_message_id,
                    "thread_id": thread.thread_id,
                    "seq": 2,
                    "role": "assistant",
                    "content": thread.answer,
                    "missing_aspects": list(thread.missing_aspects),
                    "created_at": thread.updated_at,
                },
            )
        ],
    )
    await connection.execute(
        insert(AgentMessage),
        [
            row
            for fixture in CONTINUITY_FIXTURES.values()
            for row in (
                {
                    "id": fixture.completed_user_message_id,
                    "thread_id": fixture.thread_id,
                    "seq": 1,
                    "role": "user",
                    "content": fixture.completed_question,
                    "missing_aspects": [],
                    "created_at": fixture.updated_at,
                },
                {
                    "id": fixture.assistant_message_id,
                    "thread_id": fixture.thread_id,
                    "seq": 2,
                    "role": "assistant",
                    "content": fixture.answer,
                    "missing_aspects": list(fixture.missing_aspects),
                    "created_at": fixture.updated_at,
                },
                {
                    "id": fixture.active_user_message_id,
                    "thread_id": fixture.thread_id,
                    "seq": 3,
                    "role": "user",
                    "content": fixture.active_question,
                    "missing_aspects": [],
                    "created_at": fixture.updated_at,
                },
            )
        ],
    )
    alpha = FIXTURE_THREADS[0]
    await connection.execute(
        insert(AgentMessageSource),
        [
            {
                "id": 9_000_000_000_000 + ordinal,
                "message_id": alpha.assistant_message_id,
                "ordinal": ordinal,
                "kind": "external_url",
                "source_ref": f"S{ordinal}",
                "analyzed_article_id": None,
                "url": f"https://example.com/e2e/research-alpha/source-{ordinal}",
                "title": (
                    f"E2E source {ordinal:02d}: 長いソースタイトルでも折り返して"
                    "外側のdocumentへ横スクロールを発生させない"
                ),
                "source_name": "Vector E2E External Research Monitor",
                "published_at": alpha.updated_at - dt.timedelta(days=ordinal),
                "evidence_claim": (
                    "設備投資、供給制約、電力調達の公開情報を比較するための固定引用。"
                    f"この根拠はレスポンシブ表示確認用の外部ソース {ordinal} です。"
                ),
            }
            for ordinal in range(1, _ALPHA_SOURCE_COUNT + 1)
        ],
    )
    await connection.execute(
        insert(AgentMessageSource),
        [
            {
                "id": source.id,
                "message_id": fixture.assistant_message_id,
                "ordinal": source.ordinal,
                "kind": "external_url",
                "source_ref": source.source_ref,
                "analyzed_article_id": None,
                "url": source.url,
                "title": source.title,
                "source_name": source.source_name,
                "published_at": fixture.updated_at - dt.timedelta(days=source.ordinal),
                "evidence_claim": source.evidence_claim,
            }
            for fixture in CONTINUITY_FIXTURES.values()
            for source in fixture.sources
        ],
    )
    await connection.execute(
        insert(AgentRun),
        [
            {
                "id": thread.run_id,
                "thread_id": thread.thread_id,
                "user_message_id": thread.user_message_id,
                "assistant_message_id": thread.assistant_message_id,
                "status": "completed",
                "progress_stage": "synthesizing",
                "error_code": None,
                "created_at": thread.updated_at,
                "started_at": thread.updated_at,
                "completed_at": thread.updated_at,
            }
            for thread in FIXTURE_THREADS
        ],
    )
    active_started_at = dt.datetime.now(dt.UTC)
    await connection.execute(
        insert(AgentRun),
        [
            row
            for fixture in CONTINUITY_FIXTURES.values()
            for row in (
                {
                    "id": fixture.completed_run_id,
                    "thread_id": fixture.thread_id,
                    "user_message_id": fixture.completed_user_message_id,
                    "assistant_message_id": fixture.assistant_message_id,
                    "status": "completed",
                    "progress_stage": "synthesizing",
                    "error_code": None,
                    "created_at": fixture.updated_at,
                    "started_at": fixture.updated_at,
                    "attempt_epoch": 1,
                    "completed_at": fixture.updated_at,
                },
                {
                    "id": fixture.active_run_id,
                    "thread_id": fixture.thread_id,
                    "user_message_id": fixture.active_user_message_id,
                    "assistant_message_id": None,
                    "status": "running",
                    "progress_stage": "synthesizing",
                    "error_code": None,
                    "created_at": fixture.updated_at,
                    "started_at": active_started_at,
                    "attempt_epoch": 1,
                    "completed_at": None,
                },
            )
        ],
    )


def _continuity_fixture(variant: str) -> ContinuityFixture:
    try:
        return CONTINUITY_FIXTURES[variant]
    except KeyError as exc:
        raise ValueError(f"unknown continuity fixture variant: {variant!r}") from exc


async def _reset_continuity_run(
    connection: AsyncConnection,
    variant: str,
    now: dt.datetime,
) -> None:
    fixture = _continuity_fixture(variant)
    result = await connection.execute(
        update(AgentRun)
        .where(
            AgentRun.id == fixture.active_run_id,
            AgentRun.thread_id == fixture.thread_id,
            AgentRun.user_message_id == fixture.active_user_message_id,
            or_(AgentRun.status == "running", AgentRun.status == "failed"),
        )
        .values(
            status="running",
            progress_stage="synthesizing",
            error_code=None,
            assistant_message_id=None,
            completed_at=None,
            attempt_epoch=1,
            started_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise RuntimeError(
            f"continuity {variant} reset expected exactly one row, "
            f"got {result.rowcount}"
        )


async def _fail_continuity_run(
    connection: AsyncConnection,
    variant: str,
    now: dt.datetime,
) -> None:
    fixture = _continuity_fixture(variant)
    result = await connection.execute(
        update(AgentRun)
        .where(
            AgentRun.id == fixture.active_run_id,
            AgentRun.thread_id == fixture.thread_id,
            AgentRun.user_message_id == fixture.active_user_message_id,
            AgentRun.status == "running",
        )
        .values(
            status="failed",
            error_code="internal_error",
            completed_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise RuntimeError(
            f"continuity {variant} fail expected exactly one row, got {result.rowcount}"
        )


async def run(command: str, variant: str | None = None) -> None:
    if command in ("seed", "cleanup"):
        if variant is not None:
            raise ValueError(f"{command} does not accept a continuity variant")
    elif command in ("reset", "fail"):
        if variant is None:
            raise ValueError(f"{command} requires a continuity variant")
        _continuity_fixture(variant)
    else:
        raise ValueError(f"unknown E2E research command: {command!r}")

    database_url = settings.migration_database_url or settings.database_url
    engine = create_app_engine(
        database_url,
        application_name="vector-cli-seed-e2e-research",
    )
    try:
        async with engine.begin() as connection:
            match command:
                case "seed":
                    await _seed(connection)
                case "cleanup":
                    await _cleanup(connection)
                case "reset" if variant is not None:
                    await _reset_continuity_run(
                        connection,
                        variant,
                        dt.datetime.now(dt.UTC),
                    )
                case "fail" if variant is not None:
                    await _fail_continuity_run(
                        connection,
                        variant,
                        dt.datetime.now(dt.UTC),
                    )
                case _:
                    raise RuntimeError(
                        "validated E2E research command was not dispatched"
                    )
    finally:
        await engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed")
    commands.add_parser("cleanup")
    variants = tuple(CONTINUITY_FIXTURES)
    for command in ("reset", "fail"):
        command_parser = commands.add_parser(command)
        command_parser.add_argument("variant", choices=variants)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    guard_production(os.environ.get("ENV", ""))
    asyncio.run(run(args.command, getattr(args, "variant", None)))


if __name__ == "__main__":
    main()
