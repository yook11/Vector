"""中断時にも残るケース別JSON結果を、pytestの実行報告から保存する。"""

import json
from pathlib import Path


def pytest_addoption(parser):
    parser.addoption("--aws-smoke-report", help="ケース別JSON結果の保存先")


def pytest_configure(config):
    if path := config.getoption("--aws-smoke-report"):
        config.pluginmanager.register(SmokeReport(Path(path)), "smoke-results")


class SmokeReport:
    def __init__(self, path):
        self.path = path
        self.cases = {}
        self.save()

    def pytest_runtest_logreport(self, report):
        case = self.cases.setdefault(
            report.nodeid,
            {
                "name": report.nodeid,
                "status": "running",
                "phases": {},
                "properties": {},
            },
        )
        case["phases"][report.when] = report.outcome
        case["properties"].update(dict(report.user_properties))
        if "failed" in case["phases"].values():
            case["status"] = "failed"
        elif "skipped" in case["phases"].values():
            case["status"] = "skipped"
        elif report.when == "teardown":
            case["status"] = "passed"
        self.save()

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(list(self.cases.values()), ensure_ascii=False, indent=2) + "\n"
        )
        temporary.chmod(0o600)
        temporary.replace(self.path)
