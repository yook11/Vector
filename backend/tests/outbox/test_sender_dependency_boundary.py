"""Outboxの依存境界を、DB・通信を使わずPythonのimport構文から検証する。"""

import ast
from pathlib import Path


def test_outbox_does_not_import_sqs_or_sdk():
    """OutboxはSQS実装やSDKを直接importしない。"""
    app = Path(__file__).resolve().parents[2] / "app"
    forbidden = ("app.aws.sqs", "botocore", "boto3")
    violations = []
    for path in sorted((app / "outbox").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                modules = [module, *(f"{module}.{alias.name}" for alias in node.names)]
            else:
                continue
            if any(
                module == prefix or module.startswith(prefix + ".")
                for module in modules
                for prefix in forbidden
            ):
                violations.append(f"{path.relative_to(app)}:{node.lineno}")
    assert violations == []
