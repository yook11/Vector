"""re_curate_all CLI のテスト (Phase 1B α-1)。

検証する観点:

- ``build_parser``: --execute / --limit / --all / --id-from / --id-to / --max-retries
  の解釈、--limit と --all の排他
- ``run``: dry-run default, ``--execute`` 渡し / 件数選択 / id 範囲 / 0 件時 stdout
- exit code: failed_ids 0 → 0, failed_ids あり → 3
- 既存 ``ArticleCuration`` を持たない article は selection から除外される
- ``--limit`` 未指定 + ``--all`` 未指定 → デフォルト 3 件で打ち止め (誤爆防止)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.curation.ai.envelope import CurationCall
from app.analysis.curation.cli.re_curate_all import build_parser, run
from app.analysis.curation.cli.recuration_service import (
    RecurationService,
    RecurationSummary,
)
from app.analysis.curation.domain import Signal
from app.analysis.curation.repository import CurationRepository
from app.models.article import Article
from app.models.news_source import NewsSource
from tests.analysis.curation.cli.test_recuration_service import (
    _curator as make_curator,  # 再利用 (BaseCurator mock)
)


def _summary_from_stdout(captured: str) -> dict:
    """stdout (structlog 進捗ログ + 最終 JSON 行) から summary を抜き出す。"""
    for line in reversed(captured.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)["re_curate_summary"]
    raise AssertionError(f"no JSON line in stdout: {captured!r}")


class TestBuildParser:
    def test_defaults(self) -> None:
        args = build_parser().parse_args([])
        assert args.execute is False
        assert args.limit is None
        assert args.all is False
        assert args.id_from is None
        assert args.id_to is None
        assert args.max_retries == 3

    def test_execute_flag(self) -> None:
        args = build_parser().parse_args(["--execute"])
        assert args.execute is True

    def test_limit_and_all_are_mutually_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--limit", "5", "--all"])

    def test_id_range(self) -> None:
        args = build_parser().parse_args(["--id-from", "10", "--id-to", "20"])
        assert args.id_from == 10
        assert args.id_to == 20

    def test_max_retries_overridable(self) -> None:
        args = build_parser().parse_args(["--max-retries", "5"])
        assert args.max_retries == 5


# run — Article + ArticleCuration を seed して selection を検証する


async def _seed_article_with_extraction(
    db_session: AsyncSession,
    sample_source: NewsSource,
    *,
    url: str,
) -> Article:
    article = Article(
        source_id=sample_source.id,
        source_url=url,
        original_title="Original",
        original_content="content body content body",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)

    repo = CurationRepository(db_session)
    await repo.save_signal(
        CurationCall(
            result=Signal(title_ja="旧", summary_ja="旧"),
            raw_response='{"relevance":"signal"}',
            raw_relevance="signal",
            prompt_version="testver1",
            model_name="test-model",
        ),
        article_id=article.id,
    )
    await db_session.commit()
    return article


async def _seed_article_without_extraction(
    db_session: AsyncSession, sample_source: NewsSource, *, url: str
) -> Article:
    article = Article(
        source_id=sample_source.id,
        source_url=url,
        original_title="Original",
        original_content="content body content body",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


@pytest.mark.asyncio
async def test_run_default_dry_run_processes_3_articles_at_most(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--limit / --all 未指定なら先頭 3 件で打ち止め (デフォルト誤爆防止)。"""
    for i in range(5):
        await _seed_article_with_extraction(
            db_session, sample_source, url=f"https://example.com/limit-{i}"
        )

    curator = make_curator()
    service = RecurationService(session_factory)
    args = build_parser().parse_args([])
    code = await run(args, service, curator, session_factory)

    assert code == 0
    assert curator.curate.await_count == 3
    summary = _summary_from_stdout(capsys.readouterr().out)
    assert summary["success"] == 3
    assert summary["dry_run"] is True


@pytest.mark.asyncio
async def test_run_excludes_articles_without_existing_extraction(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ArticleCuration を持たない article は selection 段階で除外される。"""
    a_with = await _seed_article_with_extraction(
        db_session, sample_source, url="https://example.com/with"
    )
    await _seed_article_without_extraction(
        db_session, sample_source, url="https://example.com/without"
    )

    curator = make_curator()
    service = RecurationService(session_factory)
    args = build_parser().parse_args(["--all"])
    code = await run(args, service, curator, session_factory)

    assert code == 0
    curator.curate.assert_awaited_once()
    summary = _summary_from_stdout(capsys.readouterr().out)
    assert summary["success"] == 1
    assert summary["skipped"] == 0
    # 念のため: extractor が呼ばれたのは extraction 持ち article のみ
    _ = a_with  # noqa: F841 (seeded but not directly asserted by id)


@pytest.mark.asyncio
async def test_run_id_range_filters(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--id-from / --id-to で範囲指定された article のみ処理される。"""
    seeded = []
    for i in range(5):
        seeded.append(
            await _seed_article_with_extraction(
                db_session, sample_source, url=f"https://example.com/range-{i}"
            )
        )

    target_id = seeded[2].id
    curator = make_curator()
    service = RecurationService(session_factory)
    args = build_parser().parse_args(
        ["--all", "--id-from", str(target_id), "--id-to", str(target_id)]
    )
    code = await run(args, service, curator, session_factory)

    assert code == 0
    curator.curate.assert_awaited_once()
    summary = _summary_from_stdout(capsys.readouterr().out)
    assert summary["success"] == 1


@pytest.mark.asyncio
async def test_run_zero_targets_prints_no_targets_summary(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """対象 article が 0 件: extractor を呼ばず note=no_targets で 0 終了。"""
    curator = make_curator()
    service = RecurationService(session_factory)
    args = build_parser().parse_args(["--all"])
    code = await run(args, service, curator, session_factory)

    assert code == 0
    curator.curate.assert_not_called()
    summary = _summary_from_stdout(capsys.readouterr().out)
    assert summary["note"] == "no_targets"


@pytest.mark.asyncio
async def test_run_returns_3_when_any_failed(
    session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """failed_ids 1 件以上で exit code 3。"""

    async def _fake_execute(self, article_ids, curator, *, dry_run):  # noqa: ARG001
        return RecurationSummary(
            success_ids=(),
            failed_ids=(42,),
            skipped_ids=(),
            dry_run=dry_run,
        )

    monkeypatch.setattr(RecurationService, "execute", _fake_execute)

    # selection 側に少なくとも 1 件あるよう短絡的に noop 化する
    async def _fake_select(*args, **kwargs):  # noqa: ANN001, ARG001
        return (42,)

    monkeypatch.setattr(
        "app.analysis.curation.cli.re_curate_all._select_article_ids",
        _fake_select,
    )

    curator = make_curator()
    service = RecurationService(session_factory)
    args = build_parser().parse_args(["--execute"])
    code = await run(args, service, curator, session_factory)

    assert code == 3
    summary = _summary_from_stdout(capsys.readouterr().out)
    assert summary["failed"] == 1
    assert summary["failed_ids"] == [42]
