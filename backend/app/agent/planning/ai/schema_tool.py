"""Question planner Gemini response schema."""

from __future__ import annotations

from typing import Any, get_args

from app.agent.contract import PlanType
from app.agent.planning.contract import (
    MAX_ARTICLE_SEARCH_QUERIES,
    RESEARCH_TASK_LIMIT,
    TargetTimeWindowKind,
)

_TARGET_TIME_WINDOW_KIND_VALUES = list(get_args(TargetTimeWindowKind))

QUESTION_PLANNER_GEMINI_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "required": [
        "plan_type",
        "research_tasks",
    ],
    "properties": {
        "plan_type": {
            "type": "STRING",
            "enum": list(get_args(PlanType)),
            "description": "計画の種別。direct_answer または search。",
        },
        "research_tasks": {
            "type": "ARRAY",
            "maxItems": RESEARCH_TASK_LIMIT,
            "description": (
                "research_goalとarticle_search_queriesを対にした調査単位のリスト。"
            ),
            "items": {
                "type": "OBJECT",
                "required": [
                    "research_goal",
                    "article_search_queries",
                ],
                "properties": {
                    "research_goal": {
                        "type": "STRING",
                        "description": (
                            "その調査で何を確認したいか、何が根拠として有用かを書く"
                            "短い日本語。keyword queryは書かない。外部検索のqueryは"
                            "実行時にリサーチャーが生成する。"
                        ),
                    },
                    "article_search_queries": {
                        "type": "ARRAY",
                        "maxItems": MAX_ARTICLE_SEARCH_QUERIES,
                        "description": (
                            "内部の分析済み記事をベクトル検索するための自然文。"
                            "質問をそのままコピーせず、entity / topic / event / "
                            "time intentを抽出・圧縮する。"
                        ),
                        "items": {
                            "type": "STRING",
                        },
                    },
                },
            },
        },
        "target_time_window": {
            "type": "OBJECT",
            "nullable": True,
            "required": ["kind"],
            "description": "外部根拠の公開・更新期間の指定。",
            "properties": {
                "kind": {
                    "type": "STRING",
                    "enum": _TARGET_TIME_WINDOW_KIND_VALUES,
                },
                "year": {
                    "type": "INTEGER",
                    "minimum": 1,
                    "maximum": 9999,
                    "nullable": True,
                },
                "month": {
                    "type": "INTEGER",
                    "minimum": 1,
                    "maximum": 12,
                    "nullable": True,
                },
                "days": {
                    "type": "INTEGER",
                    "minimum": 1,
                    "maximum": 60,
                    "nullable": True,
                },
                "start_date": {
                    "type": "STRING",
                    "format": "date",
                    "nullable": True,
                },
                "end_date_inclusive": {
                    "type": "STRING",
                    "format": "date",
                    "nullable": True,
                },
            },
        },
    },
}
