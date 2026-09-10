"""pytestの環境補完に依存せずCIの独立プロセスを起動できることを検証する。"""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("workflow_name", "job_name"),
    [("ci.yml", "frontend-e2e-smoke"), ("schemathesis-nightly.yml", "schemathesis")],
)
def test_seed_import_with_workflow_environment(workflow_name, job_name, tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows" / workflow_name).read_text())
    environment = {
        key: str(value)
        for key, value in (workflow["env"] | workflow["jobs"][job_name]["env"]).items()
    }
    environment["PYTHONPATH"] = str(ROOT / "backend")
    # ローカルの設定ファイルも親pytestの環境変数も検証結果へ混ぜない。
    script = """
import runpy
import sys
from unittest.mock import patch
from pydantic_settings import DotEnvSettingsSource
with patch.object(DotEnvSettingsSource, '_read_env_files', return_value={}):
    runpy.run_path(sys.argv[1], run_name='seed_import_check')
    from app.config import settings
    assert settings.egress_proxy_url == 'http://egress-disabled.vector.internal:9'
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, str(ROOT / "backend/scripts/seed_e2e_users.py")],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
