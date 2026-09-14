# 工程別backfill Lambdaの入口と接続管理

## Problem / Evidence

DBの未完了状態から救済するbackfill本体を、工程別Lambdaで一度実行できるようにする。
既存consumerのIAM接続・単一接続pool・ログ、Outbox relayの配送定義とpublisher、本体の監査・再投入契約を利用する。

## Interface

`app.lambda_handlers.backfill` の `curation_handler`・`assessment_handler`・`embedding_handler` が `(event, context)` を受け取り、正常終了時は `None` を返す。
入力eventから工程や時刻を選ばず、各入口が起動時に一度取得したUTC時刻を本体へ渡す。

専用Settingsは `DATABASE_URL`・`AWS_REGION`・`DB_IAM_AUTH` と自工程の `SQS_ARTICLE_<STAGE>_QUEUE_URL` を要求する。
IAM認証を必須とし、パスワード入りURLの拒否とproductionのTLS必須条件は既存設定層を利用する。
`BACKFILL_CURATIONS_ENABLED`・`BACKFILL_ASSESSMENTS_ENABLED`・`BACKFILL_EMBEDDINGS_ENABLED` はそれぞれデフォルトtrue。明示的なfalseでは設定検証後に終了し、接続を生成しない。
アプリ全体のSettings・.env・AI接続設定・Redis設定を読み込まない。

## Invariants

- 呼び出しごとにRDS署名クライアント・engine・publisherを準備する。engineのpoolは1接続、overflowは0、接続・コマンド・pool待機は各5秒。
- DB接続識別名は `vector-backfill-curation`・`vector-backfill-assessment`・`vector-backfill-embedding` とする。
- session factoryは開閉だけを担い、記事単位のトランザクションと監査はbackfill本体が所有する。
- 工程別の既存イベント・本文builder・送信先をRoutedEventPublisherとSqsSenderへ渡す。SQSクライアントはSqsSenderが送信ごとに解放する。
- 正常・初期化失敗・本体失敗・キャンセル時も、取得済みengineとRDSクライアントを逆順に解放する。
- 終了単独の失敗は実行失敗とする。先行した例外やキャンセルがある場合はそれを維持する。
- 入口の失敗ログは工程・箇所・例外クラスだけを記録し、本文や設定値を含めない。ログ障害で元の例外を変更せず、本体監査も重複記録しない。
- 入口の追加リトライはなく、SQS SDKも既存の単一試行を維持する。項目別送信失敗と後続送信の扱いは本体へ委ねる。

## Non-goals / Done

AWS定義・Scheduler・デプロイ・自動再試行の設定・旧経路の停止と削除は後続スライス。
旧Taskiq設定、DB schema、consumer受信契約は変更しない。

専用設定・配線・時刻固定・解放と失敗優先順位を単体テストで確認する。
実DBの署名器のみを既存IAM fixtureで差し替え、実入口から保存済み事実をSDKスタブへ送る。
同じ入口の繰り返しでも接続とイベントループを持ち越さず、新しいイベントIDで再送できることを確認する。
Ruff lint・format check・全単体・make test-integrationの成功を完了条件とする。

## 検証結果（2026-09-14）

- Ruff lint・format check・差分の空白チェックが成功した。
- 全単体テスト6,992件、make test-integrationのDB統合テスト1,478件が成功した。
- 新規テストは単体34件と実入口からのDB統合3件で、3工程の再起動・配送事実・資源の非共有を確認した。
- テスト用DB・Redis・ネットワークは終了処理で削除した。AWSへの適用は行っていない。
