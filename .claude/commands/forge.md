---
description: Forge を起動してマルチエージェント実装プランを策定
argument-hint: <題材テキスト or 題材ファイルパス>
---

引数 `$ARGUMENTS` を題材として Forge を起動する。

## 手順

1. `$ARGUMENTS` が既存ファイルパスかチェックする (Bash の `test -f "$ARGUMENTS"` で判定)
2. **既存ファイルなら**: そのパスを forge.py に直接渡す
3. **テキスト直接なら**: Write ツールで `/tmp/forge-topic-<unix_epoch>.md` に `$ARGUMENTS` の内容を保存し、その temp file のパスを forge.py に渡す

インラインテキストを `"$ARGUMENTS"` の形でシェルに渡すと、バッククォートや `$(...)` がシェル展開される余地がある (command injection リスク)。ユーザー入力は必ず Write ツール経由で file 化してから forge.py の引数として渡し、シェル展開経路に乗せないこと。

## 実行

作業ディレクトリは `scripts/agent_lab/` (SDK venv がそこにしかない):

```bash
cd scripts/agent_lab && uv run forge.py <topic-file-path>
```

`<topic-file-path>` は上の手順 2 or 3 で決定したパス。

## 注意点

- 3 ラウンド (planner → 5 specialists 並列 → synthesizer) で 5〜30 分かかる。`run_in_background` は使わず前景で待機する
- 出力は `plans/drafts/<timestamp>-<slug>/` に v1.md / contributions/ / PLAN.md として書かれる

完了後、`PLAN.md` の絶対パスをユーザーに伝える。
