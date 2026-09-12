"""runner上の検証対象イメージで記事準備・投入・別接続からの読取を行う。"""

import asyncio
import json
import sys
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from app.db.iam import build_iam_password_provider
from app.db.ssl import split_ssl_from_url
from botocore.config import Config
from botocore.session import Session


@asynccontextmanager
async def connect(settings, role):
    host = settings["database"]["address"]
    url = f"postgresql+asyncpg://{role}@{host}:5432/vector?sslmode=require"
    _, tls = split_ssl_from_url(url)
    with closing(
        Session().create_client(
            "rds",
            region_name="ap-northeast-1",
            config=Config(proxies={}, ignore_configured_endpoint_urls=True),
        )
    ) as client:
        password = build_iam_password_provider(
            url, region="ap-northeast-1", generate_token=client.generate_db_auth_token
        )
        connection = await asyncpg.connect(
            host=host,
            port=5432,
            database="vector",
            user=role,
            password=await password(),
            **tls,
            timeout=10,
            command_timeout=10,
        )
        try:
            yield connection
        finally:
            await connection.close(timeout=5)


async def seed(settings):
    async with connect(settings, "vector") as connection:
        heads = set(
            ScriptDirectory.from_config(AlembicConfig("alembic.ini")).get_heads()
        )
        actual = {
            r["version_num"]
            for r in await connection.fetch("SELECT version_num FROM alembic_version")
        }
        if actual != heads:
            raise RuntimeError("migration_head_mismatch")
        async with connection.transaction():
            source = await connection.fetchval(
                "SELECT id FROM news_sources ORDER BY id LIMIT 1"
            )
            category = await connection.fetchval(
                "SELECT id FROM categories ORDER BY id LIMIT 1"
            )
            if source is None or category is None:
                raise RuntimeError("reference_data_missing")
            title = "試験用半導体企業が新しいAI向けチップを発表"
            summary = (
                "試験用企業がデータセンター向けAI半導体を発表し、"
                "来期の量産開始を予定している。"
            )
            article = await connection.fetchval(
                "INSERT INTO analyzable_articles "
                "(source_id, source_url, original_title, "
                "original_content, published_at) "
                "VALUES ($1, $2, $3, $4, now()) RETURNING id",
                source,
                f"https://example.com/aws-smoke/{settings['run_id']}/{uuid4()}",
                title,
                summary,
            )
            curation = await connection.fetchval(
                "INSERT INTO article_curations "
                "(analyzable_article_id, translated_title, summary) "
                "VALUES ($1, $2, $3) RETURNING id",
                article,
                title,
                summary,
            )
            analyzed = await connection.fetchval(
                "INSERT INTO analyzed_articles "
                "(curation_id, translated_title, summary, investor_take, category_id) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING id",
                curation,
                title,
                summary,
                "AI半導体需要と量産計画の進捗を確認する。",
                category,
            )
    # イベント本文は製品の変換関数を使わず、公開された契約から独立して用意する。
    return {
        "event_id": str(uuid4()),
        "event_type": "article.assessed_in_scope",
        "schema_version": 1,
        "occurred_at": datetime.now(UTC).isoformat(),
        "payload": {"curation_id": curation, "analyzed_article_id": analyzed},
    }


async def run(settings):
    operation = settings["operation"]
    if operation == "seed":
        return await seed(settings)
    if operation == "read":
        async with connect(settings, "vector_app") as connection:
            row = await connection.fetchrow(
                "SELECT embedding::text AS embedding "
                "FROM analyzed_articles WHERE id=$1",
                settings["article_id"],
            )
            if row is None:
                raise RuntimeError("target_article_missing")
            return {"embedding": row["embedding"]}
    if operation == "send":
        with closing(
            Session().create_client(
                "sqs",
                region_name="ap-northeast-1",
                config=Config(
                    connect_timeout=5,
                    read_timeout=10,
                    retries={"total_max_attempts": 1},
                    ignore_configured_endpoint_urls=True,
                ),
            )
        ) as sqs:
            result = sqs.send_message(
                QueueUrl=settings["queue_url"], MessageBody=settings["body"]
            )
            return {"message_id": result["MessageId"]}
    raise RuntimeError("unknown_probe_operation")


if __name__ == "__main__":
    try:
        result = asyncio.run(asyncio.wait_for(run(json.loads(sys.argv[1])), timeout=40))
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if len(encoded) > 20000:
            raise RuntimeError("probe_output_too_large")
        print(encoded)
    except Exception as error:
        # 接続URL・トークン・SQLパラメーターをSSMの出力へ流さない。
        details = {"error_type": type(error).__name__}
        if isinstance(error, RuntimeError) and str(error) in {
            "migration_head_mismatch",
            "reference_data_missing",
            "target_article_missing",
            "unknown_probe_operation",
            "probe_output_too_large",
        }:
            details["reason"] = str(error)
        print(json.dumps(details), file=sys.stderr)
        raise SystemExit(1) from None
