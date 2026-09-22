# 工程別backfill Lambdaの入口と接続管理

## Problem / Evidence

DBの未完了状態から救済するbackfill本体を、工程別Lambdaで一度実行できるようにする。
既存consumerのIAM接続・単一接続pool・ログ、Outbox relayの配送定義とpublisher、本体の監査・再投入契約を利用する。

## Interface

`app.lambda_handlers.backfill` の `curation_handler`・`assessment_handler`・`embedding_handler`・`completion_handler` が `(event, context)` を受け取り、正常終了時は `None` を返す。
入力eventから工程や時刻を選ばず、各入口が起動時に一度取得したUTC時刻を本体へ渡す。

専用Settingsは `DATABASE_URL`・`AWS_REGION`・`DB_IAM_AUTH` と自工程の `SQS_ARTICLE_<STAGE>_QUEUE_URL` を要求する。
IAM認証を必須とし、パスワード入りURLの拒否とproductionのTLS必須条件は既存設定層を利用する。
`BACKFILL_CURATIONS_ENABLED`・`BACKFILL_ASSESSMENTS_ENABLED`・`BACKFILL_EMBEDDINGS_ENABLED`・`BACKFILL_COMPLETIONS_ENABLED` はそれぞれデフォルトtrue。明示的なfalseでは設定検証後に終了し、接続を生成しない。
アプリ全体のSettings・.env・AI接続設定・Redis設定を読み込まない。

## Invariants

- 呼び出しごとにRDS署名クライアント・engine・publisherを準備する。engineのpoolは1接続、overflowは0、接続・コマンド・pool待機は各5秒。
- DB接続識別名は `vector-backfill-curation`・`vector-backfill-assessment`・`vector-backfill-embedding`・`vector-backfill-completion` とする。
- session factoryは開閉だけを担い、記事単位のトランザクションと監査はbackfill本体が所有する。
- 工程別の既存イベント・本文builder・送信先をRoutedEventPublisherとSqsSenderへ渡す。SQSクライアントはSqsSenderが送信ごとに解放する。
- 正常・初期化失敗・本体失敗・キャンセル時も、取得済みengineとRDSクライアントを逆順に解放する。
- 終了単独の失敗は実行失敗とする。先行した例外やキャンセルがある場合はそれを維持する。
- 入口の失敗ログは工程・箇所・例外クラスだけを記録し、本文や設定値を含めない。ログ障害で元の例外を変更せず、本体監査も重複記録しない。
- 入口の追加リトライはなく、SQS SDKも既存の単一試行を維持する。項目別送信失敗と後続送信の扱いは本体へ委ねる。

## Non-goals / Done

AWS定義・Scheduler・デプロイ・自動再試行の設定・旧経路の停止と削除は後続スライス。
旧Taskiq設定、DB schema、consumer受信契約は変更しない。

専用設定・時刻固定・起動抑止・安全なログ・接続設定の引き渡しは通常の単体テストが担当する。
全体動作と実資源管理は`local_tests/backfill/`へ置き、共通system_databaseのmigration適用済みDBを使用する。
工程ごとの配送は個別ケースで確認し、再投入と共通資源管理を混ぜない。
共通資源は共通入口を直接呼び、実接続の生存期間・再利用・分離・失敗時の解放を確認する。
使用済み接続の終了は、別接続のpg_stat_activityから接続IDが消えたことまで確認する。
本体の同じ再投入保証はローカル側へ集約し、Redis・Outbox不使用と各部品の条件表は通常テストに維持する。
Ruff lint・format check・全単体・make test-integration・make test-localの成功を完了条件とする。

## 先行コミットの検証結果（2026-09-14）

- Ruff lint・format check・全単体テスト6,992件、通常DB統合テスト1,478件が成功した。
- この時点の通常DB統合はモデルから作成するDBであり、migration適用後の保証は含んでいなかった。
- 追加コミットで上記の所有先へ移し、配送・再投入・資源管理を分離する。

## テスト整理後の検証結果（2026-09-14）

- 全単体7,073件、make test-integrationの通常DB統合1,475件、make test-localのmigration適用済みDBテスト128件が成功した。
- backfillのlocal_testsは配送・再投入4件と共通資源管理14件。工程別配送に共通資源管理のassertを含めない。
- Ruff lint・format check・差分の空白チェックが成功し、製品コードとAWS設定は変更していない。
- backend/tests/AGENTS.mdのparametrizeルール変更を同じPRに含める。
