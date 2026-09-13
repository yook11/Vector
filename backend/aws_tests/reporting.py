"""収集対象とケース別の途中結果を、pytestの実行報告から保存する。"""

import json
from pathlib import Path


def pytest_addoption(parser):
    parser.addoption("--aws-smoke-report", help="ケース別JSON結果の保存先")
    parser.addoption(
        "--aws-smoke-collection", help="収集対象とpytest終了コードの保存先"
    )


def pytest_configure(config):
    if path := config.getoption("--aws-smoke-report"):
        config.pluginmanager.register(
            SmokeReport(Path(path), config.getoption("--aws-smoke-collection")),
            "smoke-results",
        )


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


class SmokeReport:
    def __init__(self, path, collection_path):
        self.path = path
        self.collection_path = Path(collection_path) if collection_path else None
        previous = json.loads(path.read_text()) if path.exists() else []
        self.cases = {case["name"]: case for case in previous}
        self.collection = {"nodeids": [], "issues": [], "exit_code": None}
        self.save()

    def case(self, nodeid):
        return self.cases.setdefault(
            nodeid,
            {
                "name": nodeid,
                "status": "not_run",
                "phases": {},
                "properties": {},
            },
        )

    def pytest_collectreport(self, report):
        if report.failed or report.skipped:
            self.collection["issues"].append(
                {"name": report.nodeid, "status": report.outcome}
            )
            self.save()

    def pytest_collection_finish(self, session):
        self.collection["nodeids"] = [item.nodeid for item in session.items]
        for nodeid in self.collection["nodeids"]:
            self.case(nodeid)
        self.save()

    def pytest_runtest_logstart(self, nodeid, location):
        self.case(nodeid)["status"] = "running"
        self.save()

    def pytest_runtest_logreport(self, report):
        case = self.case(report.nodeid)
        case["phases"][report.when] = report.outcome
        case["properties"].update(dict(report.user_properties))
        if hasattr(report, "wasxfail"):
            case["status"] = "xpassed" if report.passed else "xfailed"
        elif "failed" in case["phases"].values():
            case["status"] = "failed"
        elif case["status"] in {"xpassed", "xfailed"}:
            pass
        elif "skipped" in case["phases"].values():
            case["status"] = "skipped"
        elif case["status"] not in {"xpassed", "xfailed"} and case["phases"] == {
            "setup": "passed",
            "call": "passed",
            "teardown": "passed",
        }:
            case["status"] = "passed"
        self.save()

    def pytest_sessionfinish(self, session, exitstatus):
        self.collection["exit_code"] = int(exitstatus)
        self.save()

    def save(self):
        save(self.path, list(self.cases.values()))
        if self.collection_path:
            save(self.collection_path, self.collection)
