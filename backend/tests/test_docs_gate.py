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
