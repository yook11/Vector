"""実DBテストだけでIAM署名結果をローカルDBの認証情報へ差し替える。"""

from types import ModuleType
from unittest.mock import Mock

from pytest import MonkeyPatch
from sqlalchemy.engine import URL, make_url

from app.db import iam


def inject_test_db_signer(
    monkeypatch: MonkeyPatch,
    database_url: str,
    *,
    resources_module: ModuleType | None = None,
) -> str:
    """製品側のIAM経路を維持し、AWS署名器だけをテスト用に置き換える。"""
    url = make_url(database_url)
    password = url.password
    if not password or not url.host or not url.username:
        raise ValueError("test DB signer requires a host, user and password")

    def generate_token(*, DBHostname: str, Port: int, DBUsername: str) -> str:  # noqa: N803
        if (DBHostname, Port, DBUsername) != (
            url.host,
            url.port or 5432,
            url.username,
        ):
            raise AssertionError("IAM signing target differs from test DB connection")
        return password

    def create_rds() -> Mock:
        rds = Mock(spec_set=["generate_db_auth_token", "close"])
        rds.generate_db_auth_token.side_effect = generate_token
        return rds

    if resources_module is None:
        monkeypatch.setattr(iam, "_rds_client", lambda region: create_rds())
    else:

        def create_client(service_name: str, **kwargs: object) -> Mock:
            if service_name != "rds":
                raise AssertionError("test DB signer only replaces the RDS client")
            return create_rds()

        session = Mock(spec_set=["create_client"])
        session.create_client.side_effect = create_client
        monkeypatch.setattr(resources_module, "Session", Mock(return_value=session))
    return URL.create(
        drivername=url.drivername,
        username=url.username,
        host=url.host,
        port=url.port,
        database=url.database,
        query=url.query,
    ).render_as_string(hide_password=False)
