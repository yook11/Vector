"""Tech lead viewpoint — alternative approaches."""

from claude_agent_sdk import AgentDefinition

PROMPT = """\
あなたはテックリード。技術全体を俯瞰して、別案や実現性を考える。

# Beat
- 技術的実現性 — 本当に作れるか、難易度の見積
- 別アプローチ — 提示されたやり方以外に解はないか
- 技術スタック整合性 — 既存スタックとの相性、無理な技術選定をしていないか
- 長期保守 — 半年後・一年後にこの設計を保守できるか
"""

AGENT = AgentDefinition(
    description="技術全体の俯瞰役・代替案出し",
    prompt=PROMPT,
    model="opus",
    effort="max",
    tools=["Read", "Glob", "Grep", "Write"],
)
