"""実DBテストだけでIAM署名結果をローカルDBの認証情報へ差し替える。"""

from unittest.mock import Mock

from sqlalchemy.engine import URL, make_url


def inject_test_db_signer(monkeypatch, resources_module, database_url):
    """製品側のIAM経路を維持し、AWS署名器だけをテスト用に置き換える。"""
    url = make_url(database_url)
    rds = Mock()
    rds.generate_db_auth_token.return_value = url.password
    session = Mock()
    session.create_client.return_value = rds
    monkeypatch.setattr(resources_module, "Session", Mock(return_value=session))
    return URL.create(
        drivername=url.drivername,
        username=url.username,
        host=url.host,
        port=url.port,
        database=url.database,
        query=url.query,
    ).render_as_string(hide_password=False)
