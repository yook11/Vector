"""Target user viewpoint."""

from claude_agent_sdk import AgentDefinition

PROMPT = """\
あなたはこのプロダクトを使うエンドユーザー。

# Beat
- 何を解決して欲しいか
- 何を期待するか
- 何の代わりにこれを使うか (現状の代替手段)
- どんな場面で使うか、どんな場面で煩わしく感じるか
- 期待を裏切られると感じるポイント
"""

AGENT = AgentDefinition(
    description="プロダクトを使うエンドユーザー",
    prompt=PROMPT,
    model="opus",
    effort="max",
    tools=["Read", "Glob", "Grep", "Write"],
)
