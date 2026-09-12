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

構築・migration適用・結果回収・自動削除は、外側の`make aws-smoke`が担当する。[操作手順](../../infra/aws-test/README.md#実行コマンドの使い方)を参照する。
実行コマンドが次を完了してから、このテストを呼び出す。

1. Managerで試験設備を構築し、LambdaのイメージdigestとTerraform出力が一致することを確認する。
2. runnerに同じbackendイメージをdigest指定でpullし、`/etc/vector-test/proxy.env`とSSM Agentの準備を完了する。
3. 試験RDSに初期ロール・拡張・認証スキーマ・最新Alembic migration・参照データを既存の正本から適用する。
4. `vector`と`vector_app`のIAM接続、LambdaのSQSトリガー有効化、試験専用AIキーを用意する。
5. `terraform output -json`をローカルへ保存し、失敗・中断時も設備削除へ進む外側の処理を用意する。

記事準備時にも、検証対象イメージのAlembic headsとDBのrevision一致を確認する。`create_all`・`stamp`・追加GRANTで前提不足を補わない。
準備は`vector`、保存結果の読取は毎回新しい`vector_app`接続を使う。トークン生成と証明書・ホスト名検証は製品の共通実装を使う。
SSM経由で試験用probeだけを一時マウントし、製品コードは変更しない。コンテナはhostネットワークでrunnerのIAM資格情報を利用する。

## 実行コマンド内部の呼出例

以下はDB準備後の内部呼出に相当し、このpytest単体には設備削除処理がない。通常はリポジトリルートの`make aws-smoke`を使う。依存は既存のbackend開発環境を利用する。

```sh
cd backend
.venv/bin/python -m pytest aws_tests/embedding/test_event_processing.py \
  --aws-smoke-outputs ../infra/aws-test/.local/20260912-01-outputs.json \
  --aws-account-config ../infra/aws-test/.local/account.json \
  --aws-runner-profile vector-test-runner \
  -o junit_family=xunit1 \
  --junitxml=../infra/aws-test/.local/20260912-01-results.xml
```

起動時に期待アカウント・Runnerの認証先・試験用リソース名を照合し、不一致は失敗にする。
ローカルの2ケース全体で300秒の待機期限を共有する。SDK通信には別途短いタイムアウトを設けるため、終了には処理中の通信時間が加わり得る。
SSMのprobeもコンテナ実行45秒、コマンド実行50秒、配信60秒の上限を持つ。ローカル中断時にSSMへ投入済みの処理が即時停止したとは扱わず、外側の削除処理がその終了を考慮する。

実行コマンドは`reporting`プラグインを明示してケースごとのJSON結果も逐次保存する。JUnit XMLには各ケースの成否、run ID、ソースrevision、backend digest、SSM Command ID、SQS message ID、確認した完了理由を残す。
SSM応答はサイズ制限内の小さいJSON結果だけに使い、診断ログの全量回収はCloudWatchから行う外側の回収処理に任せる。
CloudWatchへの反映が期限に間に合わない場合も、確認不能として失敗にする。キューの概算件数や空のログを成功の根拠にしない。
データは専用の破棄予定DB内に残し、診断情報を回収してから外側で環境全体を削除する。

今回はユーザー指示によりテスト未実行。構文・lint・formatだけを確認し、実AWSでの疎通や合格を保証したものではない。
