"""AgentCore Gateway の web-search tool をどう呼ぶかの宣言。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from app.agent.evidence_collection.external_search.contract import (
    ExternalSearchDateFilter,
)

__all__ = [
    "AGENTCORE_WEB_SEARCH_SPEC",
    "AgentCoreWebSearchSpec",
    "build_tool_arguments",
    "build_tool_call_payload",
]


@dataclass(frozen=True, slots=True)
class AgentCoreWebSearchSpec:
    """AgentCore Gateway の MCP endpoint と tool の呼び出し設定。"""

    mcp_path: str
    tool_name: str
    request_timeout_seconds: int
    max_results_limit: int
    start_date_backoff_days: int


AGENTCORE_WEB_SEARCH_SPEC: Final[AgentCoreWebSearchSpec] = AgentCoreWebSearchSpec(
    mcp_path="/mcp",
    # Gateway が公開する名前には target 名がプレフィックスされる。AWS docs の
    # configurations[].name は `WebSearch` なので、docs からは導出できない。
    # 実機の tools/list で確認した値 (probe 2026-08-30)。
    tool_name="web-search___WebSearch",
    # 実測 0.5s (probe 2026-08-30、3 回)。service 側の
    # PROVIDER_SEARCH_TIMEOUT_SECONDS より短くして、時間切れが分類済み reason として
    # span に残るようにする (外側の wait_for で切れると provider_failed しか残らない)。
    request_timeout_seconds=10,
    # inputSchema の "Valid range: 1-25" (probe 2026-08-30)。
    max_results_limit=25,
    # 期間は JST 基準で解決される (time_filter.py) が、publishedDateFilter は UTC。
    # JST の 1 日は UTC で前日 15:00 から始まるため、from をそのまま渡すと
    # 開始日の JST 午前が範囲から漏れる。境界のヒットを落とさない側へ倒す。
    start_date_backoff_days=1,
)


def build_tool_arguments(
    spec: AgentCoreWebSearchSpec,
    *,
    query: str,
    limit: int,
    date_filter: ExternalSearchDateFilter | None,
) -> dict[str, object]:
    """specの固定値と1回分の条件からtool argumentsを組み立てる。"""
    arguments: dict[str, object] = {
        "query": query,
        "maxResults": min(limit, spec.max_results_limit),
    }
    if date_filter is not None:
        # publishedDateFilter は **両端 inclusive** (inputSchema の記載、probe
        # 2026-08-30)。ExternalSearchDateFilter は半開区間なので、末尾は
        # end_date の前日になる。
        start_date = date_filter.start_date - timedelta(
            days=spec.start_date_backoff_days
        )
        arguments["filters"] = {
            "publishedDateFilter": {
                "from": start_date.isoformat(),
                "to": (date_filter.end_date - timedelta(days=1)).isoformat(),
            }
        }
    return arguments


def build_tool_call_payload(
    arguments: dict[str, object], *, name: str
) -> dict[str, object]:
    """MCP の tools/call 1 回分の JSON-RPC payload。

    Gateway は stateless なので initialize handshake は要らない (probe 2026-08-30)。
    id は 1 呼び出し 1 リクエストなので固定でよい。
    """
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
