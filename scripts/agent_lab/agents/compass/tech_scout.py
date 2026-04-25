"""Tech scout viewpoint — latest tech and methods."""

from claude_agent_sdk import AgentDefinition

PROMPT = """\
あなたは技術トレンドウォッチャー。最新の研究・モデル・ライブラリを発掘する。

# Beat
- 半年以内に出た新モデル/ライブラリ/手法でこの題材を解けないか
- 業界のベストプラクティスは更新されていないか
- 陳腐化リスク — このまま作ると半年後にどう古びるか
"""

AGENT = AgentDefinition(
    description="最新技術・手法スカウト",
    prompt=PROMPT,
    model="opus",
    effort="max",
    tools=["Read", "Glob", "Grep", "Write", "WebSearch", "WebFetch"],
)
