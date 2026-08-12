"""captured stdout から EMF レコードを抽出するテスト用読み手。

``emit_metric`` の wire format (stdout 1 行 JSON・``_aws`` キーが EMF の定義)
の読み手を 1 か所に集約する。format の契約自体は tests/cloudwatch/test_emf.py
が固定し、形式が変わればこの読み手も一緒に変わる。
"""

from __future__ import annotations

import json
from typing import Any


def emf_records(captured_stdout: str) -> list[dict[str, Any]]:
    """stdout から全 EMF レコード (``_aws`` キー行) をパースして返す。"""
    records: list[dict[str, Any]] = []
    for line in captured_stdout.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and "_aws" in record:
            records.append(record)
    return records


def metric_records(captured_stdout: str, metric_name: str) -> list[dict[str, Any]]:
    """stdout から metric_name を持つ EMF レコードだけを抽出する。"""
    return [record for record in emf_records(captured_stdout) if metric_name in record]
