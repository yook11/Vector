"""実DB用署名器が接続先の誤りを隠さず、テストの外へ残らないことを検証する。"""

import pytest
from sqlalchemy.engine import make_url

from app.db import iam
from app.lambda_handlers.embedding import resources
from tests.iam_fixtures import inject_test_db_signer

DATABASE_URL = (
    "postgresql+asyncpg://vector_app:test%40password%2Fvalue@"
    "localhost:15432/vector_test_gw1?sslmode=require"
)


@pytest.fixture(params=[None, resources], ids=["shared-client", "invocation-client"])
def signer(request, monkeypatch):
    module = request.param
    original = iam._rds_client if module is None else module.Session
    with monkeypatch.context() as patch:
        url = inject_test_db_signer(patch, DATABASE_URL, resources_module=module)
        generate_token = (
            None
            if module is None
            else module.Session().create_client("rds").generate_db_auth_token
        )

        def provider(target):
            return iam.build_iam_password_provider(
                target, region="ap-northeast-1", generate_token=generate_token
            )

        yield url, provider
    assert (iam._rds_client if module is None else module.Session) is original


@pytest.mark.asyncio
async def test_preserves_connection_target_and_provides_decoded_password(signer):
    url, provider = signer
    parsed = make_url(url)
    assert parsed.password is None
    assert parsed.set(password=make_url(DATABASE_URL).password) == make_url(
        DATABASE_URL
    )
    provide = provider(url)
    assert await provide() == "test@password/value"
    assert await provide() == "test@password/value"


@pytest.mark.parametrize(
    "change",
    [{"host": "wrong.invalid"}, {"port": 5432}, {"username": "vector_auth"}],
)
@pytest.mark.asyncio
async def test_rejects_signing_for_another_connection(signer, change):
    url, provider = signer
    target = make_url(url).set(**change).render_as_string(hide_password=False)
    with pytest.raises(AssertionError, match="signing target differs"):
        await provider(target)()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://vector_app@localhost/vector_test",
        "postgresql+asyncpg://vector_app:test@/vector_test",
        "postgresql+asyncpg://localhost/vector_test",
    ],
)
def test_rejects_missing_test_credentials_before_patching(monkeypatch, url):
    original = iam._rds_client
    with pytest.raises(ValueError, match="requires a host, user and password"):
        inject_test_db_signer(monkeypatch, url)
    assert iam._rds_client is original
