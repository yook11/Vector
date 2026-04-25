---
description: Compass を起動して多視点ブレインストーミング (What を考える、実装プラン化はしない)
argument-hint: <題材テキスト or 題材ファイルパス>
---

引数 `$ARGUMENTS` を題材として Compass を起動する。

## 役割

Compass は「**何を作るか (What)**」を多視点から発散議論するツール。
実装プラン化はしない (それは /forge の役割)。

## 手順

1. `$ARGUMENTS` が既存ファイルパスかチェックする (Bash の `test -f "$ARGUMENTS"` で判定)
2. **既存ファイルなら**: そのパスを compass.py に直接渡す
3. **テキスト直接なら**: Write ツールで `/tmp/compass-topic-<unix_epoch>.md` に `$ARGUMENTS` の内容を保存し、その temp file のパスを compass.py に渡す

インラインテキストを `"$ARGUMENTS"` の形でシェルに渡すと、バッククォートや `$(...)` がシェル展開される余地がある (command injection リスク)。ユーザー入力は必ず Write ツール経由で file 化してから compass.py の引数として渡し、シェル展開経路に乗せないこと。

## 実行

作業ディレクトリは `scripts/agent_lab/` (SDK venv がそこにしかない):

```bash
cd scripts/agent_lab && uv run compass.py <topic-file-path>
```

`<topic-file-path>` は上の手順 2 or 3 で決定したパス。

## 注意点

- 2 ラウンド (10 specialists 並列 → synthesizer) で 5〜30 分かかる。`run_in_background` は使わず前景で待機する
- 出力は `discussions/drafts/<timestamp>-<slug>/` に topic.md / contributions/ / DISCUSSION.md として書かれる

完了後、`DISCUSSION.md` の絶対パスをユーザーに伝える。
