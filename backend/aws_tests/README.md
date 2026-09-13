# AWS Embeddingスモークテスト

準備済みのテスト専用AWS環境へ、Runnerプロファイルから実イベントを投入する。
通常の`pytest`の探索対象には含めず、このディレクトリを明示したときだけ実行する。
実SQS・Lambda・Gemini・IAM認証付きRDSを利用し、AI応答やDB接続は差し替えない。

## 確認する2ケース

- `test_event_saves_embedding_for_target_article`：未生成の記事へイベントを投入し、`saved`完了後に別のアプリ用DB接続から768次元の有限・非ゼロベクトルを確認する。
- `test_redelivery_keeps_saved_embedding`：そのケース専用の記事を初回保存してから同じイベント本文を再投入し、新しいSQSメッセージIDが`already_embedded`で完了した後、保存結果が完全一致することを確認する。

各ケースは独立した記事とイベントIDを用意するため、片方だけでも実行できる。2件を通す場合は合計3回SQSへ投入する。
ログはmessage ID・event ID・分析記事ID・完了理由を照合する。未処理のままベクトルが変わっていない状態や、初回の完了ログだけでは合格にしない。
本文整形・再配送時のAI呼出回数・同時処理・ロールバック・接続解放は既存のローカルテストが担当する。

## 実行前提

構築は`make aws-smoke-up`、DB準備は`make aws-smoke-prepare`、対象指定の試験は`make aws-smoke-test`、削除は`make aws-smoke-destroy`が担当する。一括実行には`make aws-smoke TEST=aws_tests/embedding/`を使う。[操作手順](../../infra/aws-test/README.md#実行コマンドの使い方)を参照する。
実行コマンドが次を完了してから、このテストを呼び出す。

1. Managerで試験設備を構築し、LambdaのイメージdigestとTerraform出力が一致することを確認する。
2. runnerに同じbackendイメージをdigest指定でpullし、`/etc/vector-test/proxy.env`とSSM Agentの準備を完了する。
3. 試験RDSに初期ロール・拡張・認証スキーマ・最新Alembic migration・参照データを既存の正本から適用する。
4. `vector`と`vector_app`のIAM接続、LambdaのSQSトリガー有効化、試験専用AIキーを用意する。
5. `terraform output -json`と固定入力をRUN_ID配下へ保存する。

記事準備時にも、検証対象イメージのAlembic headsとDBのrevision一致を確認する。`create_all`・`stamp`・追加GRANTで前提不足を補わない。
準備は`vector`、保存結果の読取は毎回新しい`vector_app`接続を使う。トークン生成と証明書・ホスト名検証は製品の共通実装を使う。
SSM経由で試験用probeだけを一時マウントし、製品コードは変更しない。コンテナはhostネットワークでrunnerのIAM資格情報を利用する。

## 対象を指定して実行する

準備を完了したRUN_IDを使い、リポジトリルートで実行する。依存は既存のbackend開発環境を利用する。

```sh
make aws-smoke-test RUN_ID=20260912-01 TEST=aws_tests/embedding/
make aws-smoke-test RUN_ID=20260912-01 \
  TEST=aws_tests/embedding/test_event_processing.py::test_redelivery_keeps_saved_embedding TIMEOUT=600
# 新規環境の作成から削除までまとめる場合
make aws-smoke TEST=aws_tests/embedding/
```

`TEST`は`aws_tests`配下のディレクトリ・ファイル・pytest node IDを1つ指定する。一括実行でも省略できない。AWS操作前に、実行用コピーから対象を60秒以内で収集し、0件・収集エラー・収集時skipは拒否する。モジュールのimportや収集ではAWS接続を行わず、実行時fixtureへ接続処理を置く。

実行時の未コミット変更・追加を含むテスト、conftest、補助コード、fixtureファイルを`infra/aws-test/.local/runs/<RUN_ID>/test-attempts/<連番>/code/`へコピーする。fixtureも`aws_tests`内に置き、隠しファイル・キャッシュ・ログ・範囲外へのシンボリックリンクを使わない。実行中の元コード編集は混入せず、次の実行に反映する。テストのGit revision・ハッシュは各回の`source.json`へ記録し、環境・アプリイメージの固定revisionとは分ける。

起動時に期待アカウント・Runnerの認証先・試験用リソース名を照合し、不一致は失敗にする。
指定対象のpytest起動からsetup・call・teardown全体で`TIMEOUT`（正の整数秒、既定300秒）の期限を共有する。Embedding runtimeにも同じ期限を渡す。SDK通信には別途短いタイムアウトがあり、プロセス終了待ちが加わる場合がある。
SSMのprobeもコンテナ実行45秒、コマンド実行50秒、配信60秒の上限を持つ。ローカル中断時にSSMへ投入済みの処理が即時停止したとは扱わず、再実行・削除時も前回の処理が続いている可能性を考慮する。

実行コマンドは`reporting`プラグインを明示してケースごとのJSON結果も逐次保存する。収集した1件以上の全ケースが成功し、pytest終了コードが0の場合だけ合格とし、skip・xfail・未完了を合格に数えない。各回にJSON・JUnit・pytestログ・CloudWatchログと回収成否を残す。回収失敗時はケース合否と分けてコマンドを非0で終了する。JUnit XMLには各ケースの成否、run ID、ソースrevision、backend digest、SSM Command ID、SQS message ID、確認した完了理由を残す。
SSM応答はサイズ制限内の小さいJSON結果だけに使い、診断ログの全量回収はCloudWatchから行う外側の回収処理に任せる。
CloudWatchへの反映が期限に間に合わない場合も、確認不能として失敗にする。キューの概算件数や空のログを成功の根拠にしない。
個別実行は成功・失敗・中断でも環境を保持し、次回の試験に同じRUN_IDを使える。データは専用DB内に残す。試験を終えたら表示された`aws-smoke-destroy`を実行する。一括実行は診断情報の回収後に環境全体を削除する。

実行制御はAWSへ接続しないpytest subprocessテストで検証する。実AWS・実AIを利用する上記Embeddingケースの合否は、後続のAWS実行で確認する。
