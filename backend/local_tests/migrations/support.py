"""隔離DBへ実際のAlembic revisionを適用する。"""

from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

from local_tests.database import ROOT


async def migrate(database, operation, revision):
    engine = create_async_engine(database.url("vector", sqlalchemy=True))

    def run(connection):
        config = Config(str(ROOT / "backend/alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "backend/alembic"))
        config.attributes["connection"] = connection
        operation(config, revision)

    try:
        async with engine.connect() as connection:
            await connection.run_sync(run)
    finally:
        await engine.dispose()
