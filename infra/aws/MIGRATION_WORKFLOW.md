# Migrationとアプリ反映の導入・運用

## 現在の導入境界

コード上ではmigration専用workflowとアプリ反映を分離し、旧migration入口・自動dispatch・
freezeを撤去した。通常のCI roleへのSSO入口はplan / pushだけに限定し、
apply / migration / rolloutは承認付きworkflowへ集約した。
本番のGitHub設定変更、IAM適用、Terraform apply、dispatchは未実施。
**以下の切り替えを完了するまで、新workflowを本番で実行しない。**

Terraformが管理するひな型は `${name_prefix}-migration-base` だけとし、
実行用 `${name_prefix}-migration` はcontrollerが登録する。毎回baseの最新ACTIVE revisionを
検証・複製し、image digest・mode・revision・tree OIDを実行用にだけ足す。
前回の実行用定義を次回のひな型にせず、baseに実行要求envを書き戻さない。

## 本番切り替えチェックリスト

切り替え中のdispatchとローカル本番操作は人手で止め、旧run・承認待ちを空にしてから進める。
既存runは古いworkflowと資格情報を保持するため、コードmergeだけで閉鎖済みとは扱わない。

1. 既存の `production` と、新設する `production-migration` / `production-rollout` の
   3つすべてを確認する。reviewerは `yook11`、Prevent self-reviewはオフ、admin bypassはオフ、
   Deployment branchesは `main` のみ。未設定Environmentの自動作成に頼らない。
2. `AWS_MIGRATION_ROLE_ARN` を `production-migration`、`AWS_ROLLOUT_ROLE_ARN` を
   `production-rollout` に置く。`production` のapply用secretも確認し、
   `AWS_PUSH_ROLE_ARN` はbuild専用repository secretに残す。
3. コードをmergeし、管理者がbootstrap IAMを適用する。applyは既存の `production` を維持し、
   migration / rolloutのOIDC subjectを専用Environmentに切り替える。
   SSOの `DeployPermissionSet` はplan / pushだけに残す。
   migrationにはcontroller用の `ecs:ListServices` / `ecs:DescribeServices` を追加し、
   人手の `rds-db:connect` を除く。rolloutはアプリtask / execution roleのみPassRole可能とし、
   `RunTask` と直接DB接続権限を持たせない。migrateに `UpdateService` は付与しない。
4. trustの反映を確認した時刻と、apply / migration / rollout roleの切替前の最大session時間から
   待機期限を記録し、既発行の一時資格情報が期限切れになるまで通常運用を再開しない。
   期限を判断できなければ切り替えを完了扱いにしない。profile削除・ログアウト・trust変更だけで
   既発行のSTS資格情報まで失効したとは扱わず、自動失効処理は追加しない。
   [AWSのsession管理仕様](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_revoke-sessions.html)
5. 承認付き `AWS terraform apply` で本体を適用し、`migration-base` の作成と
   旧Terraform管理ひな型の撤去を確認する。以降も通常の本体applyはこのworkflowを使う。
   apply / migration / rolloutのEnvironment付き本番変更jobだけが共通group
   `vector-production-change`、`cancel-in-progress: false`、`queue: max` に参加することを確認する。
   buildは含めず、キュー順を適用順序の保証には使わない。
   旧 `production` Environmentのmigration / rollout secretと旧freeze変数を削除する。
6. ローカルからapply / migration / rollout roleを新規assumeできず、plan / pushは
   引き続きassumeできることを確認する。profile検証スクリプトの拒否だけで代用しない。
   通常の `VectorDeploy` に、これらのCI roleを経由しない直接の本番変更権限が
   Identity Center側で付与されていないことも確認する。未確認・想定外の権限があれば再開しない。
7. 承認付きapplyの結果を確認し、新runnerを含む最新main SHAでmigration workflowの
   `verify` を実行・承認する。
   imageのprotocol・内容検証に成功し、実DBがそのSHAの単一headと一致するときだけ
   新形式ledgerが成功する。初回ledger不在は許容するが、初期DB構築は行わない。
8. 最新mainのアプリ反映を別承認し、migration taskを起動しないことと全serviceの完了を確認する。
   verify後にmainが進んだ場合もschema照合が必要で、schemaが変われば先にmigrationを行う。
   本番の承認・OIDC・ECS接続確認まで終えてから、本番切替完了とする。

本番有効化はこの手順を別途完了した時点とし、ローカル検証の成功だけで完了扱いにしない。
個人のAWS設定・資格情報・CLIキャッシュは実装で変更しない。
管理者のbootstrap・初期構築・非常時復旧は残すが、通常の承認失敗をadminで迂回しない。
applyのインフラ管理権限は維持しており、管理者や承認済みインフラ変更の能力を完全に封じる
構成ではない。保証するのは、通常権限から承認なしで本番変更用roleを取得する入口の閉鎖である。

## 通常の操作と承認

### Infra

PRのplan確認 → mainへmerge → `AWS terraform apply`の`production`承認で進める。
infra変更のmain pushによる自動起動と、mainからの手動再実行は維持する。
ローカルではplan / pushだけを使い、`vector-apply` / `vector-migrate` / `vector-rollout`は
profile検証でもAWS呼び出し前に拒否する。通常applyを管理者実行へ置き換えない。

### Migration

`AWS DB migration` をmainから手動実行し、40桁の `release_sha` と
`expand` / `contract` / `verify` の `mode` を明示する。対象SHAはmain履歴上に限定する。

prepareのsummaryを開き、SHA・mode・target head・migration tree OID・ledger由来の
期待開始revision・baseline ID・予定range・分類理由を確認してから
`production-migration` を承認する。承認画面にsummaryが自動表示されるわけではない。
承認前はDB接続もledger書き込みも行わず、表示はlive DBの観測値ではない。
復旧verifyで利用可能なledgerが無ければ開始revision・rangeは未確認とし、
API障害でbaselineを確認できない場合は復旧扱いにしない。

expandは先に適用し、その後アプリを反映する。contractは**互換コードを最新mainとして
本番へ反映してからcontract PRをmergeする**。contractの対象SHAの直前main SHAが
proxy以外の全serviceへ完全反映済みで、ledgerもそのschemaに一致していることを必要とする。
直前SHAとの差分は下記の許可pathに限定し、CIと本番prepareで同じ規則を使う。
mixedはCIとrunnerで拒否し、条件を満たすためのapp rolloutはmigration workflowから実行しない。

### Contract PRの変更範囲

変更したmigrationにcontractが含まれる場合だけ、PR全体に混在制限を適用する。
過去のcontractの存在やテスト名では発動しない。PRではbaseとheadのmerge-baseからheadまで、
main pushではbeforeから対象SHAまでを確認し、差分やmigration内容を確認できない場合は拒否する。
rename前の削除pathも確認するため、実行コードをテスト名へ移動しても制限の対象になる。

| 変更内容 | 混在制限 |
| --- | --- |
| テストのみ・テスト＋文書・アプリ＋テスト | 対象外。通常のCIを実行 |
| expand＋アプリ＋テスト | 許可 |
| contractのみ・contract＋許可対象のテスト／文書 | 許可 |
| contract＋実行コード／制御コード／設定 | 拒否 |
| expand＋contract、未分類・不正なmigration | 拒否 |

contractに同梱できるpathは以下に固定する。

- `backend/alembic/versions/**`
- `backend/tests/**`（fixture・helper・`conftest.py`を含む）
- `frontend/src/**/*.{test,spec}.{ts,tsx}`
- `frontend/src/test/**`
- `frontend/e2e/**`（fixture・テスト専用setupを含む）
- `frontend/vitest.setup.client.ts`、`frontend/vitest.setup.node.ts`
- `**/*.md`

上記以外は拒否する。runner、workflow、Terraform、`pyproject.toml`、依存定義・lockfile、
Vitest／Playwright設定は、テスト目的でもcontract PRには含めない。
contractがない通常PRではこの混在制限を適用しない。
これはpathの契約であり、テストのimport関係や任意のCI改変まで検証する仕組みではない。

### アプリ反映

`AWS app images` をmainから入力なしで手動実行する。dispatch時の `github.sha` に固定し、
backend / frontend imageを作成または再利用した後、`production-rollout` で独立承認する。
承認後・AWS資格情報取得前と、最初のservice更新直前に最新main・CI / Security・ledgerを確認する。
mainが進んでいれば新しいrunでやり直し、service更新開始後は固定SHAの完了まで検証する。

アプリ反映は最新ledgerが新形式successで、対象SHAのtarget revisionとmigration treeが
両方一致する場合だけ許可する。ledgerのrelease SHA一致は要求せず、DBに直接問い合わせない。
記録なし・利用不可・API障害・schema不一致では停止し、migrationやledger修復を自動実行しない。
過去SHAを指定するrollbackは提供せず、修正・revertをmainへmergeして反映する。

両workflowのCI / Security判定は対象SHAのmain push workflowの最新run / attemptの成否であり、
PR専用テストのskipを含む。「そのmain SHAでunitを実行済み」を意味しない。
Environment付きOIDC subjectにはbranch名が無いため、workflowのmain制限と
EnvironmentのDeployment branches=mainを両方維持する。

## 失敗と復旧

migration imageは新規buildでも既存tagでもprotocolと実ファイルを検証し、digestで固定する。
prepare / build artifactは同じrunのIDで受け渡し、保存は30日。
期限切れ・欠損・run attempt不一致では新しいdispatchから再準備する。

承認後にbaseline・CI / Security・contract前提を再検証し、ledgerの作成・GET・
`in_progress` が完了してからECSを起動する。runnerは同じDB sessionのlock内で
live current・head・pending rangeを再検証する。実revision・rangeは構造化ログで確認し、
prepareの予定値と混同しない。

非ゼロ終了、タイムアウト、cancel、状態不明をsuccessにせず、cleanupはそのrun attemptのtaskだけを
対象にする。ledger更新に失敗した場合は未完了記録が後続を止める。task停止とDB状態を確認して
必要なら新しい `verify` を準備・承認する。古いsuccessへの復帰・自動再実行は行わない。
空のexpand / contractは `no_changes` で後続をskipし、記録だけ必要ならverifyを使う。

アプリ反映失敗時も自動rollback・migrationは行わず、状態を確認して最新mainの新しいrunを使う。
空DBの初期構築手順の再設計は今回の切り替えに含めない。
