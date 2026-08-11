"""CloudWatch Embedded Metric Format (EMF) の stdout emit。

EMF 仕様に沿った 1 行 JSON を stdout に書くと、awslogs ドライバ経由で
CloudWatch Logs に届いた時点でメトリクスとして自動抽出される。
送信 SDK も追加の egress 経路も不要で、既存のログ経路にそのまま乗る。
"""

from __future__ import annotations

import json
import sys
import time

# 本アプリの EMF メトリクスは全て単一 namespace に置く (spec §2)。
PIPELINE_NAMESPACE = "Vector/Pipeline"


def emit_count(
    metric_name: str,
    *,
    dimensions: dict[str, str],
    value: int = 1,
) -> None:
    """count メトリクス 1 打点を EMF 1 行として stdout へ書く。"""
    record: dict[str, object] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": PIPELINE_NAMESPACE,
                    "Dimensions": [list(dimensions)],
                    "Metrics": [{"Name": metric_name, "Unit": "Count"}],
                }
            ],
        },
        metric_name: value,
        **dimensions,
    }
    print(json.dumps(record, separators=(",", ":")), file=sys.stdout, flush=True)
