"""Shared prompt frame for Forge specialists."""

COMMON_RULES = """\

# 責務
v1.md を読み、自軸の観点から精度を上げる貢献を出す。

# 行動規約
- 題材 / v1.md / コードベースを Read/Glob/Grep で把握する
- 推測で書かない、根拠は常に file:line で引用
- 自軸に集中し、他ロール領域は深入りしない
- 自軸と無関係な題材なら冒頭で "N/A — reason: ..." のみ書いて終了する
- 自軸外で他 specialist に検討してほしい点は末尾に「他ロールへの論点」として記録する

# Output
指示されたパスに Markdown で Write する。
"""


def build_prompt(identity: str, beat: list[str]) -> str:
    """Specialist prompt を identity + beat から合成する。"""
    beat_bullets = "\n".join(f"- {b}" for b in beat)
    return f"""\
{identity}

# Beat(自軸)
{beat_bullets}
{COMMON_RULES}"""
