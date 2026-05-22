"""FastAPI 自動 docs (/docs, /redoc, /openapi.json) の production gate テスト。

red-team S-EXFIL-1 / C3 amplifier 防御の構造的不変条件:
- development では /docs / /redoc / /openapi.json が 200 を返す
- production では同 path が 404 を返す (FastAPI が router を物理生成しない)

実装変更で settings.env 経路や FastAPI() 引数が壊れた場合に CI で reject する
ためのガード。docs URL は module ロード時に決定されるため、test 関数ごとに
app.config / app.main を reload して env を切替える。
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def reload_app_with_env(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[str], FastAPI]]:
    """ENV を切替えて app.config / app.main を reload するヘルパー fixture。

    test 終了後 default の "development" 状態に reload して戻し、他 test に
    docs URL の設定が漏れないようにする (docs URL は module-level で評価される)。
    """

    def _reload(env_value: str) -> FastAPI:
        monkeypatch.setenv("ENV", env_value)
        if env_value == "production":
            # config.py の production narrowing で revalidate 宛先は *.flycast 必須。
            # docs gate の検証に集中するため flycast 値を入れて Settings 構築を通す。
            monkeypatch.setenv(
                "INTERNAL_FRONTEND_BASE_URL",
                "http://your-vector-frontend-app.flycast:3000",
            )
        from app import config, main

        importlib.reload(config)
        importlib.reload(main)
        return main.app

    yield _reload

    # teardown: monkeypatch が ENV を戻した後で再 reload して clean state に戻す
    monkeypatch.delenv("ENV", raising=False)
    from app import config, main

    importlib.reload(config)
    importlib.reload(main)


@pytest.mark.unit
def test_docs_endpoints_enabled_in_development(
    reload_app_with_env: Callable[[str], FastAPI],
) -> None:
    app = reload_app_with_env("development")
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200
        assert client.get("/openapi.json").status_code == 200


@pytest.mark.unit
def test_docs_endpoints_disabled_in_production(
    reload_app_with_env: Callable[[str], FastAPI],
) -> None:
    app = reload_app_with_env("production")
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


# OpenAPI operation の HTTP method 集合
# (path-level の `parameters` / `summary` 等を除外するため)
_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


@pytest.mark.unit
def test_openapi_declares_400_for_all_operations(
    reload_app_with_env: Callable[[str], FastAPI],
) -> None:
    """全 operation の OpenAPI spec に default 400 response が宣言されることを確認する。

    FastAPI が UTF-8 不正 body 等に対して内部生成する HTTPException(400) を、
    app level の responses 引数で default 宣言している
    (Schemathesis status_code_conformance finding 対応 / PR-C1a')。
    endpoint が増えても自動カバーされる構造を維持するための回帰ガード。
    """
    app = reload_app_with_env("development")
    spec = app.openapi()

    missing = [
        f"{method.upper()} {path}"
        for path, path_item in spec["paths"].items()
        for method, operation in path_item.items()
        if method in _HTTP_METHODS and "400" not in operation["responses"]
    ]

    assert missing == []
