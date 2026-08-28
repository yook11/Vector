"""research_handoff工程のGemini response schema。

文字数上限はschemaのkeyではなくdescriptionで伝え、超過分は正規化側でclampする
(既存工程と同じ方針)。上限の正本は`ORGANIZED_TEXT_MAX_CHARS`。
"""

from __future__ import annotations

from typing import Any, Final

from app.agent.research_handoff.handoff import ORGANIZED_TEXT_MAX_CHARS

__all__ = ["RESEARCH_HANDOFF_GEMINI_SCHEMA"]

_LENGTH_RULE = f"{ORGANIZED_TEXT_MAX_CHARS}字以内。"

_COLLECTED_OVERVIEW_DESCRIPTION = (
    f"{_LENGTH_RULE}"
    "このthreadの調査でこれまでにどういう記事が集まっているかを日本語で書く。"
    "件数や傾向、どの時期・どの発信源のものが多いかが読めるようにする。"
    "採用されなかった記事も集まった材料として数える。"
)
_UNRESOLVED_POINTS_DESCRIPTION = (
    f"{_LENGTH_RULE}"
    "ユーザーの要望に対して、まだ得られていない情報を日本語で書く。"
    "検索したが得られなかったこと、検索そのものが失敗したことも含める。"
    "この2つは次の打ち手が変わるため、どちらなのかが読めるようにする。"
)
_NEXT_SEARCH_GUIDANCE_DESCRIPTION = (
    f"{_LENGTH_RULE}"
    "このthreadで検索してみて分かったことを、次の調査への申し送りとして日本語で書く。"
    "どういうqueryが当たったか、どの方向は掘っても出なかったか、何に気をつけるか。"
    "何を調べるかを決めるのは後段の役割なので、計画そのものは書かない。"
)

RESEARCH_HANDOFF_GEMINI_SCHEMA: Final[dict[str, Any]] = {
    "type": "OBJECT",
    "required": [
        "collected_overview",
        "unresolved_points",
        "next_search_guidance",
    ],
    "properties": {
        "collected_overview": {
            "type": "STRING",
            "description": _COLLECTED_OVERVIEW_DESCRIPTION,
        },
        "unresolved_points": {
            "type": "STRING",
            "description": _UNRESOLVED_POINTS_DESCRIPTION,
        },
        "next_search_guidance": {
            "type": "STRING",
            "description": _NEXT_SEARCH_GUIDANCE_DESCRIPTION,
        },
    },
}
