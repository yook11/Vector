# 認証カウンターの定期掃除

> 2026-09-20: digest入力と`*_state`入力は廃止した。以下は構築時の記録で、現在の扱いは[app rollout](./app-rollout.md)を参照する。

## 責務と期限

`vector-auth-rate-limit-cleanup` Lambdaは、Better Authの`auth."rateLimit"`から、呼び出し開始時点で`lastRequest`が10分より古い行を削除する。境界時刻の行は残す。時刻はUnixミリ秒とし、一回のパラメーター付きDELETEをtransactionで確定する。イベント入力から期限・件数・接続先を変更できない。

専用ロック、バッチ上限、待機、アプリ内retryは設けない。再実行ではその呼び出し開始時点の期限で残った対象を削除する。予約同時実行数1はDBへの負荷制限であり、処理の正しさは排他に依存しない。

## 接続と失敗

専用DBユーザー`vector_auth_rate_limit_cleanup`でIAM認証・TLS接続する。既存のDBロール・権限migrationを利用し、ユーザー名や認証方式の変更を受け付けない。取得するのはDELETEの実行件数だけで、key・countを参照しない。

接続待機5秒、SQL15秒、行ロック待機3秒、Lambda30秒。呼び出し単位のNullPool接続とIAM署名器は成功・失敗時とも解放する。成功ログはcommitと資源解放後にだけ出す。失敗はLambdaへ固定文言の例外として返し、ログには段階と例外種別を記録する。接続URL、IAMトークン、元例外文、行データは出力しない。

## 実行と監視

- 既存backendイメージを利用し、arm64・512MiBで配置する。
- EventBridge Schedulerは毎時20分・50分（UTC）、入力`{}`、初期無効。
- Schedulerの配信retryは0回。Lambdaの関数エラーretryは最大2回、イベント有効期限600秒。
- 最終失敗は既存SNS alertsへ送る。Lambdaのイベント破棄・失敗送信先への配送失敗、Schedulerの配信失敗をCloudWatchで監視する。
- 成功ログ`auth_rate_limit_cleanup_completed`にrequest ID・deleted・cutoff_ms・duration_msを記録する。
- 失敗ログ`auth_rate_limit_cleanup_failed`にrequest ID・phase・error_typeを記録する。

## 配置と切り替え

1. bootstrapで専用Lambda・Schedulerの権限境界とデプロイ権限を反映する。
2. AWS app imagesでbackendイメージを作成する。AWS terraform applyに`auth_rate_limit_cleanup_image_digest`と`auth_rate_limit_cleanup_state=disabled`を指定し、新Lambdaを配置する。
   このapplyで旧analysis taskの`vector_auth` IAM接続権限も外れるため、APP rolloutまで旧掃除は失敗し得る。Schedulerは停止中なので、この短い移行区間は掃除処理が実行されない前提で連続して切り替える。
3. 新Lambdaを手動実行し、ログで正常終了を確認する。本番には確認用データを追加せず、実データの期限切れ行を処理する。0件も正常。
4. APP rolloutで旧Taskiqの認証カウンター掃除・cron削除を反映する。旧処理の実行中・キュー残件を確認し、残件があれば処分せず切り替えを保留する。
5. AWS terraform applyで`auth_rate_limit_cleanup_state=enabled`を指定する。新しいdigestを指定しない場合は現在値を維持する。
6. 次の定期実行成功、削除件数・処理時間、認証エラー、RDSメモリを確認する。

Terraformの入力が未指定なら、配置済みdigestとScheduler有効状態を引き継ぐ。他Lambdaのdigest・有効状態は変更しない。新LambdaのDB権限以外の変更をplanで確認する。

旧maintenance workerの認証掃除専用接続とIAM権限は撤去するが、監査履歴の掃除と共有workerは残す。`AUTH_RETENTION_DATABASE_URL`は管理者CLIも利用するため、設定そのものは残す。

障害時はSchedulerを無効にする。旧経路へ戻す必要がある場合は旧コード・接続設定・IAM権限を明示的に復元する。自動fallbackは設けない。
