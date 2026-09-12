# SQSメッセージの形式検証と工程別handlerの配置

## Problem

SQSメッセージの形式検証を共通で使い、各工程の処理をその場で読める構成にする。反復・1件処理・診断まで共通関数へ渡す設計は、利用者の意図を超えて呼び出しを複雑にしていたため撤去する。

## Evidence

- 既存のlambda_handlers/sqs/records.pyにSqsRecordBatchとSqsRecordがあり、両handlerはこの形式検証を共有している。
- assessment-consumer.mdとembedding-consumer.mdは、全ID検証の後に本文を個別検証し、構造不正と個別失敗の扱いを分ける。
- Assessmentは工程別compositionでConsumerを準備・解放している。Embeddingの準備・解放も同じ配置へ移すことが承認されている。

## Invariants

- 全IDを本文より先に検証し、配送構造・ID不正は全件未処理のまま伝播する。
- bodyの欠落・型不正は該当メッセージだけの失敗とする。
- 検証処理はJSON・イベント種別・payloadの業務上の意味を解釈しない。
- 工程別の逐次実行・失敗IDの原文と入力順・空バッチ応答・診断・キャンセル伝播を維持する。
- 資源の初期化後に入力を検証し、同一呼び出し内でConsumerを共有する。取得済み資源は逆順に解放し、利用側の例外を初期化失敗として再記録しない。

## Design

- 共通なのは既存のSqsRecordBatch.from_lambda_eventによる配送構造・IDの検証と、SqsRecord.body_textによる本文の存在・型の検証。検証条件の再実装やラッパーは追加しない。
- 各_run_*はこれらを直接呼び、forループ内にイベント解析・Consumer実行・診断・失敗ID集約を記述する。
- process_sqs_batch、SqsBatchDiagnostics、partial、コールバック用の_process_messageは設けない。応答のTypedDictは従来どおり各handlerが持つ。
- Consumerの準備・解放は工程別compositionが所有する。Embeddingのopen_embedding_consumerは既存のresources・gemini_client・consumerの初期化順、診断段階と通常例外・キャンセルの扱いを維持する。

## Non-goals

反復・1件処理・診断の共通化、新しい抽象基盤、Outbox relay・SQS送信、イベント契約、Consumer本体、DB schema、認証・認可、依存、AWS設定・デプロイは変更しない。

## Done

各handlerが共通の形式検証を直接使い、工程固有の処理を追える構成にする。形式検証の詳細はsqs/test_records.py、工程別の制御・応答・診断は各handlerテスト、資源の配線・初期化・終了はcompositionテストが担当する。ループは工程別に実装するため、その制御テストも各工程に残す。

Ruff lint・format、全単体テストとmake test-integrationで検証する。migration適用済みDBのlocal_tests・実AWS試験・デプロイは今回の検証範囲に含めない。

## 検証結果（2026-09-12）

- app全体と変更テストのRuff lint・format確認、git diff --checkが成功した。
- 全単体テスト6,634件が成功した。
- `make test-integration TEST_COMPOSE_PROJECT=vector-test-sqs-validation-20260912 PYTEST_ARGS='-x -q'`で統合テスト1,400件が成功し、一時DB・Redis・ネットワークの削除まで確認した。
- 既存の非推奨・Logfire関連の警告は残る。検証範囲外のlocal_tests・実AWS試験・デプロイは未実施。
- 形式検証は既存部品の直接利用に戻し、共通バッチ実行関数・診断Protocol・コールバックを撤去した。工程別compositionへの配置統一は維持した。
