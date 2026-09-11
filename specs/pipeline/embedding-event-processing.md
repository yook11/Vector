# Embeddingイベントの対象記事への確定保存

## Problem

イベントが指定する記事からAI入力が作られ、実SDKの応答が同じ記事へ確定保存されることを、migration適用済みDBで確認する。

## Evidence

- `EmbeddingRepository.load_ready_build_facts`: 分析記事IDを基準にsummaryとkey_pointsを読む。
- `GeminiEmbedder._call_api`: SDKのembed_contentを呼び、応答のvaluesを保存用ベクトルへ渡す。
- `backend/tests/analysis/embedding/test_embedder.py`: 実SDKのHTTP本文はrequests[].content.partsである。
- `backend/local_tests/embedding/test_invocation_resources.py`: 既存の実ハンドラー用接続・HTTP境界fixture。

## Invariants

- 内容の異なるイベントで指定していない記事を先に作り、対象記事のイベントを実ハンドラーへ渡す。
- 実際のHTTP本文に対象記事固有の本文が含まれ、イベントで指定していない記事の本文が含まれないことを確認する。
- 期待ベクトルは製品の変換関数から作らず、テスト側で独立して768要素を用意する。各値は(index - 384) / 512とし、全要素を異なる値にする。
- 実アプリ権限で処理し、応答後の別接続から対象行とイベントで指定していない記事の行の存在、対象の保存値、イベントで指定していない記事の未更新を確認する。
- 保存値はHALFVECの許容誤差0.001で比較する。

## Non-goals

リソース解放、監査、再試行、入力整形や全フィールドの網羅、製品コード・schema・権限の変更、AWS/GCPへの実通信。

## Done

- `embedding/test_event_processing.py`に正常系1ケースを追加する。
- 接続設定とHTTP差し替えは`embedding/conftest.py`、記事準備と実ハンドラー呼び出しは`embedding/support.py`で共有し、別テストファイルからfixtureをimportしない。
- 既存リソーステストの観測と期待値は維持する。
- ユーザー指示によりテスト実行は行わず、静的チェックのみ行う。
