---
description: Forge を起動してマルチエージェント実装プランを策定
argument-hint: <題材テキスト or 題材ファイルパス>
---

引数 `$ARGUMENTS` を題材として Forge を起動し、マルチエージェント実装プランを策定する。

以下を Bash で実行:

```bash
uv run scripts/agent_lab/forge.py "$ARGUMENTS"
```

注意点:
- 引数はテキスト直接 or ファイルパス。ファイルが存在すれば Forge 側で自動的に読み込む
- 3 ラウンド (planner → 5 specialists 並列 → synthesizer) で 5〜30 分かかる。`run_in_background` は使わず前景で待機する
- 出力は `plans/drafts/<timestamp>-<slug>/` に v1.md / contributions/ / PLAN.md として書かれる

完了後、`PLAN.md` の絶対パスをユーザーに伝える。
