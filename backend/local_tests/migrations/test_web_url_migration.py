"""保存済みURLを正規化形へ揃えるmigrationの書き換え・重複の扱い・失敗時の保持を確認する。

期待値は WHATWG URL Standard の直列化 (pydantic-core が準拠) から直接書く。
"""

import pytest

from alembic import command
from local_tests.backfill.support import (
    seed_article,
    seed_curation,
    seed_incomplete_article,
)
from local_tests.migrations.support import migrate

REVISION = "z32_normalize_web_urls"
PREDECESSOR = "z31_grant_investigation"


@pytest.fixture
async def predecessor_database(system_database):
    await migrate(system_database, command.downgrade, PREDECESSOR)
    return system_database


async def _seed_closed_incomplete(database, url):
    async with database.connect("vector") as connection:
        return await connection.fetchval(
            "INSERT INTO incomplete_articles "
            "(url, source_id, source_name, status, observed_article, "
            "ready_at, created_at) "
            "SELECT $1, id, name, 'closed', '{}'::jsonb, now(), now() "
            "FROM news_sources ORDER BY id LIMIT 1 RETURNING id",
            url,
        )


async def _article_urls(database):
    async with database.connect("vector") as connection:
        rows = await connection.fetch("SELECT id, source_url FROM analyzable_articles")
    return {row["id"]: row["source_url"] for row in rows}


async def _incomplete_urls(database):
    async with database.connect("vector") as connection:
        rows = await connection.fetch("SELECT id, url FROM incomplete_articles")
    return {row["id"]: row["url"] for row in rows}


async def _first_source(database):
    async with database.connect("vector") as connection:
        return await connection.fetchrow(
            "SELECT id, site_url, endpoint_url FROM news_sources ORDER BY id LIMIT 1"
        )


async def _set_source_urls(database, source_id, *, site_url, endpoint_url):
    async with database.connect("vector") as connection:
        await connection.execute(
            "UPDATE news_sources SET site_url = $2, endpoint_url = $3 WHERE id = $1",
            source_id,
            site_url,
            endpoint_url,
        )


async def _version(database):
    async with database.connect("vector") as connection:
        return await connection.fetchval("SELECT version_num FROM alembic_version")


async def test_rewrites_legacy_article_urls_keeping_ids_and_children(
    predecessor_database,
):
    """古い表記の記事URLだけを正規化形へ書き換え、行の id と子の行を保つ。"""
    database = predecessor_database
    empty_path = await seed_article(database, "https://legacy.example")
    default_port = await seed_article(database, "https://port.example:443/a")
    non_ascii = await seed_article(database, "https://enc.example/China’s")
    empty_path_query = await seed_article(database, "https://query.example?p=1")
    normalized = await seed_article(database, "https://norm.example/a")
    curation = await seed_curation(database, empty_path)
    incomplete = await seed_incomplete_article(database, "https://incomplete.example")

    await migrate(database, command.upgrade, REVISION)

    assert await _article_urls(database) == {
        empty_path: "https://legacy.example/",
        default_port: "https://port.example/a",
        non_ascii: "https://enc.example/China%E2%80%99s",
        empty_path_query: "https://query.example/?p=1",
        normalized: "https://norm.example/a",
    }
    assert await _incomplete_urls(database) == {
        incomplete: "https://incomplete.example/"
    }
    async with database.connect("vector") as connection:
        assert (
            await connection.fetchval(
                "SELECT analyzable_article_id FROM article_curations WHERE id = $1",
                curation,
            )
            == empty_path
        )


async def test_rewrites_site_url_to_web_url_form(predecessor_database):
    """ソースの site_url を WebUrl の正規化形へ書き換える。"""
    database = predecessor_database
    source = await _first_source(database)
    await _set_source_urls(
        database,
        source["id"],
        site_url="https://site.example",
        endpoint_url=source["endpoint_url"],
    )

    await migrate(database, command.upgrade, REVISION)

    assert (await _first_source(database))["site_url"] == "https://site.example/"


async def test_deletes_legacy_incomplete_when_new_form_exists(predecessor_database):
    """新しい形の未完成の行が既にあれば、状態に関係なく古い形の行だけを削除する。"""
    database = predecessor_database
    closed_legacy = await _seed_closed_incomplete(database, "https://closed.example")
    closed_kept = await _seed_closed_incomplete(database, "https://closed.example/")
    open_legacy = await seed_incomplete_article(database, "https://open.example")
    open_kept = await seed_incomplete_article(database, "https://open.example/")

    await migrate(database, command.upgrade, REVISION)

    assert await _incomplete_urls(database) == {
        closed_kept: "https://closed.example/",
        open_kept: "https://open.example/",
    }
    assert closed_legacy not in await _incomplete_urls(database)
    assert open_legacy not in await _incomplete_urls(database)


async def test_keeps_legacy_article_when_new_form_exists(predecessor_database):
    """新しい形の記事の行が既にあれば、古い形の行は削除も書き換えもしない。"""
    database = predecessor_database
    legacy = await seed_article(database, "https://gap.example")
    kept = await seed_article(database, "https://gap.example/")
    rewritten = await seed_article(database, "https://other.example")

    await migrate(database, command.upgrade, REVISION)

    assert await _article_urls(database) == {
        legacy: "https://gap.example",
        kept: "https://gap.example/",
        rewritten: "https://other.example/",
    }


@pytest.mark.parametrize(
    ("seed", "read"),
    [
        pytest.param(seed_article, _article_urls, id="analyzable_articles"),
        pytest.param(seed_incomplete_article, _incomplete_urls, id="incomplete"),
    ],
)
async def test_fails_without_changes_when_legacy_rows_collide(
    predecessor_database, seed, read
):
    """古い行どうしが同じ形になるなら失敗し、他の行の書き換えも取り消す。"""
    database = predecessor_database
    first = await seed(database, "https://collide.example")
    second = await seed(database, "https://collide.example:443")
    await seed(database, "https://other.example")
    before = await read(database)

    with pytest.raises(RuntimeError, match="legacy rows collide") as raised:
        await migrate(database, command.upgrade, REVISION)

    assert f"[{first}, {second}]" in str(raised.value)
    assert "collide.example" not in str(raised.value)
    assert await _version(database) == PREDECESSOR
    assert await read(database) == before


async def test_fails_without_changes_when_endpoint_url_would_change(
    predecessor_database,
):
    """一意のキーの endpoint_url が変わる行があれば失敗し、何も書き換えない。"""
    database = predecessor_database
    source = await _first_source(database)
    await _set_source_urls(
        database,
        source["id"],
        site_url="https://site.example",
        endpoint_url="https://feed.example",
    )
    article = await seed_article(database, "https://legacy.example")

    with pytest.raises(RuntimeError, match="endpoint_url: would change"):
        await migrate(database, command.upgrade, REVISION)

    assert await _version(database) == PREDECESSOR
    assert (await _first_source(database))["site_url"] == "https://site.example"
    assert await _article_urls(database) == {article: "https://legacy.example"}


async def test_second_upgrade_changes_nothing_after_round_trip(predecessor_database):
    """downgrade は何もせず、再度の upgrade は正規化済みの行を変えない。"""
    database = predecessor_database
    article = await seed_article(database, "https://legacy.example")
    incomplete = await seed_incomplete_article(database, "https://incomplete.example")

    await migrate(database, command.upgrade, REVISION)
    await migrate(database, command.downgrade, PREDECESSOR)
    await migrate(database, command.upgrade, REVISION)

    assert await _article_urls(database) == {article: "https://legacy.example/"}
    assert await _incomplete_urls(database) == {
        incomplete: "https://incomplete.example/"
    }
