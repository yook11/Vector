# Assessmentの一覧更新通知を新Consumer経路へ移す

## Problem

記事一覧の更新通知が旧Taskiq入口に残り、新Assessment Consumerの保存から呼ばれていない。旧経路の削除に先立ち、新経路から同じ通知を行えるようにする。

## Evidence

- `AssessmentService`は対象内結果・成功監査・後続Outboxのcommit後に`AssessmentCompletion(IN_SCOPE, id)`を返す。
- Lambda入口はConsumerの結果を取得してSQSの部分バッチ応答を返す。
- 既存の`FrontendRevalidateNotifier`とfrontendの`/api/internal/revalidate`はBearer認証とタグ一覧の契約を持つ。
- frontendは`articles:list`の通知成功後に新着検知用の識別子を変更する。
- Assessment実行ロールとbootstrapの権限境界は、これまで通知用SSMキーの取得を許可していない。

## Invariants

1. 新規の対象内保存が完了した場合だけ、`articles:list`と`articles:categories`を1回のPOSTで通知する。対象外・処理済み・入力拒否・保存失敗では通知用キーを取得しない。
2. 通知用キーの取得・HTTP通信の通常例外は、安全な警告ログへ降格する。保存済みの記事を通知失敗だけでSQSへ失敗として返さない。外部キャンセルは伝播する。
3. 必須の通知設定が未指定なら処理開始前に拒否する。秘密値・SDK例外の自由文は診断へ出さない。
4. 通知先はTerraformがCloud Mapのfrontend URLとして設定し、利用者入力を使用しない。リダイレクトや環境proxyを使用しない。
5. DBトランザクションと業務Consumerへfrontend依存を持ち込まない。既存Bearer認証、frontendの応答・画面仕様を維持する。

## Non-goals

旧Assessmentのworker・scheduler・cronの削除、カテゴリー初期化確認の移設、DLQの再投入、通知の永続化・再送、新規secret・依存・DB schema、AWSへの適用は含めない。

## Done

通知の配線・設定・IAM・通信経路を定義し、旧Taskiqの通知呼び出しを外す。通知条件、保存後の通知障害、権限範囲のテストとbackend・Terraformの検証が成功する。

## 実装

- `composition.py`で通知設定、SSMキー取得関数、HTTP transport、`ArticleListUpdateNotifier`を組み立てる。必須設定の読み込みはバッチ処理前、キー取得は保存成功後の通知時に行う。
- Lambda入口は`IN_SCOPE`の結果で`notify_article_list_updated()`を呼ぶ。通知クラスは一覧とカテゴリーのタグを決め、共通transportがHTTPと通知失敗の境界を担当する。
- 共通transportの`from_settings`入口は既存briefing・trendのため維持する。エラーログは例外クラスだけとし、ログ出力障害も保存結果を変えない。
- Lambdaの通知先はTerraformで確定する。Fly・開発環境を扱う共通URL型や追加のURL検証は導入せず、既存アプリの設定・テストを変更しない。
- Lambdaの実行ロールと権限境界に既存`/<prefix>/frontend/revalidate-bearer-secret`の`ssm:GetParameter`だけを追加する。Terraformに秘密値を渡さない。
- ConsumerとfrontendのSG間にTCP 3000の送受信ルールを追加する。既存SGの再作成を避けるため、SG自体のdescriptionは維持する。

## 通知失敗時の扱い

`frontend_revalidate_failed`を診断し、設定・権限・到達性を修復する。記事処理の再実行は通知の再送方法にしない。キャッシュの期限切れによる再取得はあるが、表示中画面への新着通知が再送される保証はない。次の新規保存時の通知で一覧全体を無効化できる。通知専用の永続再送は今回追加しない。

## マージ後の本番反映順序

1. bootstrapのplanでAssessment Consumerの権限境界に既存通知キー1件の取得権限だけが追加されることを確認し、管理者の手順でapplyする。
2. mainの`AWS terraform apply`で通知先・SSMパス・実行ロール・SGルールを反映する。通常applyは既存のLambda digestを維持する。
3. マージ後のbackendイメージを`AWS app images`で作成し、対象digestを確認する。このworkflowのAPP rolloutだけではLambdaのイメージは更新されない。
4. `AWS terraform apply`を手動実行し、`assessment_consumer_image_digest`だけに新digestを指定する。他工程のdigestは既存値を維持する。権限・環境変数を先に準備し、新コードだけを先行反映しない。
5. 新規対象内保存で`frontend_revalidate_ok`と2タグを確認し、通知前後の`/api/news/revision`、表示中一覧の案内と手動更新を確認する。対象外・重複で通知されないこと、SQS処理と既存DLQ件数も確認する。
6. 本番通知の確認後、旧Assessment専用runtimeの削除へ進む。

旧Taskiqへの新規投入停止を前提に切り替える。今回のローカル検証は実AWSのSSM取得・IAM・ネットワーク・frontend到達を保証しない。

## 検証の配置と結果

- `local_tests/assessment/test_event_processing.py`で実保存後の通知、通知時点でのcommit確定、対象外・保存失敗・入力拒否での非通知、HTTP 500・通信エラー・SSMキー取得障害でも保存とSQS成功応答を維持することを確認する。
- `local_tests/assessment/test_duplicate_processing.py`で再配送時に追加通知しないことを確認する。
- `tests/test_shared/test_revalidate.py`はHTTPの宛先・認証・本文、通常例外の降格、安全な診断、診断障害、外部キャンセルの通信部品としての契約を確認する。
- 旧Taskiqの通知テストを削除し、同じ業務上の通知条件をConsumer入口の単体テストで重ねない。
- `ruff check`・`ruff format --check`が成功した。backend単体一式は成功し、テスト配置整理後の関連単体36件、Assessmentローカル15件も成功した。`make test-integration PYTEST_ARGS='-x -q'`は1,424件成功した。
- Terraformはmain・bootstrapともvalidate成功（mainは既存の非推奨警告あり）。mainの全50ケースは、初回に43件成功し、追加SGのmock IDを補ったAssessmentの再実行7件が成功した。bootstrapは全31件成功した。変更ファイルのfmt確認も成功した。
- 本番へは未適用。AWS上の権限・SSM・frontend到達とブラウザの確認は、前述の反映手順で行う。
