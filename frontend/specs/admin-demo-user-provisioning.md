# 管理者によるデモ用一般ユーザー発行

> 作成日: 2026-07-22
>
> Status: Implemented
>
> 対象: frontend の Better Auth / admin UI / server-side provisioning / backend operator CLI
>
> 前提仕様: `frontend/specs/invite-only-public-signup-shutdown.md`

## Problem

Vectorはポートフォリオとして、面接や個別の要望があった場合に限って利用者へアカウントを
渡す。一般公開のセルフサービス登録は必要ないが、公開signupを停止したため、新しい利用者が
ログインできるアカウントをapplication上から発行する経路も存在しない。

本仕様では、既存の`admin`ユーザーだけが、名前・メールアドレス・パスワードを指定して
通常の`user`アカウントを発行できるようにする。発行した認証情報は、管理者が面接相手などへ
application外の個別経路で直接共有する。

ここでいう「招待制」は、招待メールや招待tokenを発行するシステムを意味しない。第三者が
自分で登録することはできず、管理者が必要な相手のアカウントだけを手動発行する運用を指す。

この管理機能を利用するには、最初に1人の`admin`が必要である。本仕様では、自分用の既存
`user`アカウントを、DB認証情報を管理する運用者がCLIで`admin`へ昇格する。初期アカウント自体の
新規作成は行わず、対象ユーザーが存在しない場合はCLIを失敗させる。以後のデモ用一般ユーザーは、
そのadminがapplicationの専用画面から発行する。

## Evidence

| 項目 | 確認内容 |
|---|---|
| Public signup | `emailAndPassword.disableSignUp: true`により、`POST /api/auth/sign-up/email`は公開ユーザーを作成しない |
| Existing sign-in | email/password sign-inは有効で、既存ユーザーと発行済みユーザーが同じログイン画面を利用する |
| Roles | `user.role`は`user` / `admin`のallowlistへnarrowingされ、未知値は`user`へfail-safeに縮退する |
| Admin page gate | admin route segmentは`requireAdmin()`、admin mutationは`requireAdminForAction()`を利用できる |
| Existing admin UI | `/settings`、`/admin/pipeline-status`、`/admin/source-health`は存在するが、ユーザー発行画面はない |
| Auth DB boundary | runtimeは`AUTH_DATABASE_URL`の`pg.Pool`を使用し、`auth` schemaへ接続する |
| Existing schema write | E2E用`seed_e2e_users.py`は`auth."user"`と`auth.account`を1 transactionで作り、credential accountの既存column契約を示している |
| Existing operator tool | `backend/scripts/promote_admin.py`は通常application用`DATABASE_URL`のengineを使うため、`vector_app`の権限分離が有効な環境では`auth."user"`のUPDATEが`InsufficientPrivilege`となり使用できない。使用例のCompose service名も現行構成と一致しない |
| DB role boundary | `vector_app`は`auth."user"`を参照できるがroleを更新できず、`vector_auth`はauth schemaの保守に必要なDML権限を持つ |
| Auth maintenance config | `AUTH_RETENTION_DATABASE_URL`は通常application接続と分離された認証保守用接続として設定層に存在し、既存のretention workerでも使用される |
| Production bootstrap | production用seedや、最初のadminをapplicationから作る経路は存在しない |
| Password compatibility | Better Auth 1.6.23は標準password実装の`hashPassword` / `verifyPassword`を`better-auth/crypto`から公開し、runtimeはcustom password設定がない場合に同じ標準実装を使う |
| ID generation | `auth.ts`は既存dependency `uuid` 14.0.1の`v7()`を`uuidv7`としてimportし、Better AuthのIDをJavaScript側で生成する |
| Rate limit | Server ActionのPOST requestは`proxy.ts`のapplication mutation rate limitを通る |
| CSRF | Next.js Server ActionはOriginとHostを比較し、action内で認証・認可を再検証することが前提である |
| Admin Plugin | `better-auth` 1.6.23に同梱済みだが、server / clientのどちらにも未導入 |

Better Auth Admin Pluginの既定`admin` roleは、ユーザー作成だけでなく、一覧、role変更、ban、
削除、password変更、session操作、impersonationなどの権限を持つ。また、同versionの
`createUser`は`auth."user"`を先に作り、その後`auth.account`を作るが、この2操作をendpoint内で
transactionへ包んでいない。後段だけ失敗すると、同じメールアドレスで再試行できない
credentialなしのuserが残り得る。

今回必要なのは、既存adminが一般ユーザーのuser / credential accountを同時に作る1操作だけで
ある。利用しない管理endpointとPlugin用columnを追加するより、既存の`pg`接続を使う専用の
server-only transactionへ責務を限定する方が、公開surfaceと失敗状態の両方を小さくできる。

この経路はBetter Authのcore tableへapplication-owned writeを行うため、Better Auth 1.6.23の
schemaとcredential契約へversion-boundとなる。column契約のtestと、作成したcredentialで実際に
Better Auth sign-inできるintegration testを変更検知の境界にする。`package.json`はcaret rangeを
使うため、明示的なupgradeだけでなく、`package-lock.json`の更新や`npm audit fix`でBetter Authの
解決versionが変わる場合も同じ確認を必要とする。

一次情報:

- [Better Auth Admin Plugin](https://www.better-auth.com/docs/plugins/admin)
- [Better Auth Database](https://www.better-auth.com/docs/concepts/database)
- [Next.js Data Security](https://nextjs.org/docs/app/guides/data-security)

## Decisions

### 1. 初期adminの昇格は運用者CLIだけに限定する

最初のadminは、自分用として既に登録済みの通常ユーザーを、`backend/scripts/promote_admin.py`で
明示的に昇格して準備する。CLIはユーザーを新規作成せず、対象メールアドレスの既存ユーザーが
見つからない場合は何も変更せず非zeroで終了する。

CLIは通常application用`settings.database_url`やmigration owner用
`settings.migration_database_url`を使用しない。`settings.auth_retention_database_url`から
短命の専用engineを作り、認証保守用DB roleの権限で対象roleだけを更新する。処理後は成否に
かかわらずengineをdisposeする。この設定が未指定の場合は、DBへの接続・query・更新を行う前に
失敗させ、別の接続先へfallbackしない。通常application用
`vector_app`へauth userのUPDATE権限を追加しない。

入力メールアドレスはtrimしてlowercaseへ正規化し、完全一致する1ユーザーだけを対象とする。
昇格先はCLI内部のliteral `admin`、`--demote`時の降格先はliteral `user`へ固定する。同じroleへ
変更する操作は成功扱いのno-opとし、対象なし、DB接続失敗、権限不足、更新失敗ではroleを変更しない。

このCLIはDB secretへアクセスできる運用者が、local shellまたはAWSの一時的なoperator taskから
手動実行する。常駐process、scheduler、HTTP route、Server Action、管理画面からは起動できない。
接続URL、password、tokenを引数、標準出力、error、logへ出さない。Composeでの実行例は現行の
`backend` service名へ更新する。

Better Auth Admin Pluginの`adminUserIds`は採用しない。最初のadminを準備する唯一の入力経路は
このoperator CLIとし、application内にはadminの作成・昇格経路を設けない。

### 2. 管理者用の3項目フォームから一般ユーザーを発行する

管理者用ページとして`/admin/users/new`を追加し、`/settings`から導線を設ける。入力項目は次の
3つだけとする。

| 項目 | 契約 |
|---|---|
| 名前 | 必須。trim後1〜100文字。Unicodeの文字、数字、空白、`-`、`_`を許可する |
| メールアドレス | 必須。trimして形式検証し、lowercaseへ正規化する |
| パスワード | 必須。8〜128文字。管理者が利用者へ直接共有するログイン用passwordとして扱う |

password確認欄、role選択、期限、備考、招待メール送信は設けない。発行したアカウントは
email verificationを要求せず、登録直後から既存のemail/passwordログインを利用できる。

8 / 128のclient-safeな値は、runtime、Better Auth schema CLI、provisioning schemaが参照する
共有password policyをSSoTとする。現在暗黙の既定値である最大128文字もruntime設定へ明示する。

password hashは独自実装しない。clientからimportできないserver-only moduleで、Better Auth
1.6.23が`better-auth/crypto`から公開する`hashPassword` / `verifyPassword`をそのままre-exportし、
provisioning transactionはその`hashPassword`を使用する。scryptのparameter、正規化、salt、保存
形式をapplication側で再実装しない。1.6.23の標準関数が内包するscrypt
`N=16384 / r=16 / p=1`、NFKC正規化、`salt:key`のhex形式はupstreamの実装詳細として扱い、
application側へ複製・固定しない。

runtimeの`emailAndPassword.password`にはcustom `hash` / `verify`を設定せず、Better Auth標準動作を
維持する。同じ関数を明示注入するだけの設定変更も行わない。互換性は関数の複製や設定の同一性では
なく、変更前から存在するcredentialとprovisioningで作成したcredentialの両方が実Better Auth
handlerでsign-inできるintegration testによって保証する。

password変更・resetを今回提供しないため、管理者が認証情報を控えないまま作成すると回復できない。
formに`認証情報をコピー`を設け、現在入力されているメールアドレスとpasswordだけをbrowserの
clipboardへコピーできるようにする。手動で控える場合も含め、`認証情報を控えました`の確認を
必須とし、未確認の間は登録buttonを有効にしない。メールアドレスまたはpasswordを変更したら
確認状態を解除し、変更後の情報を再確認させる。

clipboardへの書き込みは管理者の明示操作だけで行い、serverへ追加送信せず、client logや
永続的なclient storageへ保存しない。clipboard APIが失敗した場合は成功表示を出さず、手動で
控えたうえで確認checkを行えるようにする。

送信中は入力値を保持したまま入力欄とbuttonを無効化し、spinnerと`登録中…`を表示する。
失敗時は入力値を保持し、既知の重複メールは日本語で通知する。未知の内部エラーは詳細を返さず
genericな失敗文言にする。成功時は`一般ユーザーを登録しました`と対象メールアドレスを表示し、
formと確認状態を空にする。

plaintext passwordはaction response、success message、URL、server log、client logへ含めない。
server / client logにはpassword hashと利用者のメールアドレスも含めない。

### 3. PageとServer Actionの両方でadminを検証する

pageは既存のadmin route segmentに置き、表示前に`requireAdmin()`を通す。mutationは専用の
Server Actionだけとし、最初に`requireAdminForAction()`、次に共有schemaのserver-side
validationを行う。

Server ActionはUIから参照されていても直接POST可能な境界として扱う。pageでformを隠すことを
認可に数えず、action内のauthoritativeなDB sessionとrole検証を必須とする。未認証は既存の
login redirect、role=`user`は`Forbidden`とし、どちらもDB transactionを開始しない。

新しいRoute HandlerやBetter Auth管理endpointは追加しない。外部から到達するmutationを
Server Action 1つに限定し、Next.jsのOrigin / Host checkと`proxy.ts`のapplication mutation
rate limitを維持する。

### 4. Userとcredential accountを1つのPostgreSQL transactionで作る

専用のserver-only provisioning serviceは、既存のauth用`pg.Pool`からclientを取得し、次の順序で
処理する。

1. server-only moduleがre-exportするBetter Auth標準`hashPassword`でpasswordをhashする。hash失敗時はDB transactionを開始しない。
2. 既存`uuid` packageの`v7()`でuser IDとaccount IDをJavaScript側に生成する。
3. `BEGIN`する。
4. `auth."user"`へ通常ユーザーを1件insertする。
5. 同じuser IDを参照するcredential `auth.account`を1件insertする。
6. 両方が成功した場合だけ`COMMIT`し、それ以外は必ず`ROLLBACK`する。
7. 成否にかかわらずclientをpoolへ返す。

書き込みfieldは次に固定する。

| Table | Field | 値 |
|---|---|---|
| `auth."user"` | `id` | JavaScript側の`uuid` 14.x `v7()`で生成したUUID |
| `auth."user"` | `name` | validation済みのtrim後の値 |
| `auth."user"` | `email` | lowercaseへ正規化した値 |
| `auth."user"` | `emailVerified` | `false` |
| `auth."user"` | `role` | SQL側のliteral `user` |
| `auth."user"` | `createdAt` / `updatedAt` | 同一時刻 |
| `auth.account` | `id` | user IDとは別にJavaScript側の`uuid` 14.x `v7()`で生成したUUID |
| `auth.account` | `accountId` | user IDの文字列表現 |
| `auth.account` | `providerId` | SQL側のliteral `credential` |
| `auth.account` | `userId` | 作成したuser ID |
| `auth.account` | `password` | `better-auth/crypto`の`hashPassword`出力だけ |
| `auth.account` | `createdAt` / `updatedAt` | userと同一時刻 |

SQLは全てparameterized queryとし、role、provider、table名、column名をrequestから組み立てない。
`role`、`data`、user ID、provider ID、hash、timestampはform入力として受け付けない。

ここでいうUUIDv7は、`auth.ts`と同じ`import { v7 as uuidv7 } from "uuid"`の出力を指す。
PostgreSQL組み込みの`uuidv7()`関数は使用せず、生成済みUUIDをSQL parameterとして渡す。これにより
production databaseのPostgreSQL versionへ依存せず、新規dependencyも追加しない。

発行処理では新規ユーザー用session、verification record、emailを作成しない。操作中のadmin
sessionにもcookie変更を行わない。

メールアドレスの一意性は既存のDB unique constraintをauthoritativeな競合判定とする。同じ
メールアドレスの同時requestでは1件だけがcommitし、もう一方はtransaction全体をrollbackする。
事前の存在確認だけを正しさの根拠にしない。Better Auth CLIが生成した実schemaで
`auth."user".email`のunique constraintが存在すること自体もcontract testで固定する。

### 5. Application内にAdmin作成・昇格の入力経路を持たない

provisioning schemaはstrict objectとし、名前、メールアドレス、password以外のkeyを拒否する。
永続化するroleはrequestやapplication objectから渡さず、SQL側で`user`へ固定する。

これにより、Server Actionへ`role: "admin"`、`data.role: "admin"`、任意のIDを注入しても
validationより後へ到達せず、adminを作成できない。

運用者CLIによる既存ユーザーの明示的なrole変更と、application上のユーザー発行は別の権限境界と
して扱う。application上のadminにも、新しいadminの作成、既存ユーザーの昇格、operator CLIの
起動を許可しない。

### 6. Admin PluginとDB migrationは追加しない

今回Better Auth Admin Pluginを登録せず、`adminClient()`も追加しない。したがって、Pluginが
要求する`banned`、`banReason`、`banExpires`、`impersonatedBy` column、管理endpoint、custom
access controlは不要である。

既存のBetter Auth core tableだけへ書き込むため、Alembic migrationとBetter Auth CLI schema
変更は行わない。runtime / Better Auth schema CLIの同期契約には共有password policyの明示だけを
反映し、operator CLIのDB接続契約とは区別する。

Better Authの明示的なversion更新時だけでなく、`package-lock.json`再生成、`npm update`、
`npm audit fix`などで解決versionが変わる場合も、core user / account schemaとpassword contractの
回帰testを先に確認する。schema driftをtestで検出した場合は、provisioning SQLを憶測で追随させず、
更新versionの公式schemaと生成SQLを確認して別変更として対応する。

### 7. 発行されたアカウントは通常ユーザーと同じ制限を受ける

作成されたアカウントはrole=`user`であり、既存の認可、ユーザー単位の日次run制限、session、
rate limitをそのまま適用する。本機能を理由にquotaやAgent実行上限を緩和しない。

管理者は複数のデモアカウントを発行できるが、これは信頼済みのadmin操作として扱う。外部の
第三者はadmin sessionなしに発行できないため、同一IPからセルフサービスで複数アカウントを
作る経路にはならない。

## Invariants

1. `POST /api/auth/sign-up/email`は引き続き公開ユーザーを作成しない。
2. 最初のadminは既存ユーザーをoperator CLIで明示的に昇格して準備し、CLIは存在しないユーザーを作成しない。
3. operator CLIは`AUTH_RETENTION_DATABASE_URL`だけを使用し、未設定時に通常application用またはmigration用DB接続へfallbackしない。
4. adminの作成・昇格・降格を行えるapplication UI、Server Action、HTTP APIを追加しない。
5. 通常application用DB roleへauth userのUPDATE権限を追加しない。
6. 未認証ユーザーとrole=`user`のユーザーは、UIまたはServer Actionのどちらからも新規ユーザーを作成できない。
7. adminが本機能から作成できるroleは`user`だけである。
8. 作成requestは名前、メールアドレス、8〜128文字のpasswordだけを受け付け、role、data、IDを受け付けない。
9. 成功時はuserとcredential accountを同じtransactionで1件ずつ作り、失敗時はどちらも残さない。
10. 発行時に新規ユーザー用sessionを作成せず、操作したadminのsessionとroleを変更しない。
11. 同じメールアドレスの同時・再登録で複数ユーザーを作成しない。
12. plaintext passwordをresponse、URL、log、永続的なapplication stateへ残さず、DBにはpassword hashだけを保存する。logにはpassword hashと利用者のメールアドレスも含めない。
13. operator CLIはDB接続URLを含むsecretを標準出力、error、logへ残さない。
14. `/api/auth/admin/*`を新たに登録せず、Better Authの管理APIをbrowserへ公開しない。
15. 初期admin以外の既存ユーザー、session、role、公開登録停止の挙動を変更しない。
16. 作成されたユーザーは既存の一般ユーザー向けquotaと認可境界を迂回しない。
17. provisioningのpassword hashには`better-auth/crypto`の公開`hashPassword`を使用し、runtimeへcustom password実装を注入しない。
18. user IDとaccount IDはJavaScript側の既存`uuid` packageで生成し、PostgreSQL組み込み`uuidv7()`へ依存しない。

## Non-goals

- 公開セルフサービス登録の再開。
- 招待メール、招待token、招待URL、有効期限、再送、取消。
- passwordの自動生成、確認欄、初回変更強制、変更画面、reset機能。
- email verification。
- application UI / Server Action / HTTP APIからのadmin新規作成・role変更・admin昇格。
- CLIによる、存在しない初期ユーザーの新規作成。
- ユーザー一覧、検索、編集、停止、ban、削除。
- password再発行、session一覧・失効、impersonation。
- 発行済み認証情報をapplication内で再表示・保存する機能。
- 監査ログ基盤または通知基盤の追加。
- 日次run制限、IP制限、複数アカウント検知の変更。
- Turnstileまたは別のCAPTCHA導入。
- custom password hash algorithm、既存credentialのpassword migration。
- PostgreSQL組み込み`uuidv7()`への依存。
- Better Auth Admin Plugin、Alembic migration、新規dependencyの追加。

## Error Contract

### Application user provisioning

| 状態 | UIの扱い | 永続化 |
|---|---|---|
| 未認証 | ログイン画面へ遷移 | transactionを開始しない |
| 非admin | `Forbidden`として処理し、権限不足を通知 | transactionを開始しない |
| 認証情報の控え未確認 | 登録buttonを無効のままにする | requestを送らない |
| 入力不正 | 該当fieldへ日本語validation messageを表示 | transactionを開始しない |
| role / data / ID注入 | strict validationで拒否し、内部の検証順序は固定しない | transactionを開始しない |
| メール重複 | `このメールアドレスは登録済みです。` | 新しいuser / accountを残さない |
| user insert後のaccount失敗 | 内部情報を含まないgeneric message | transaction全体をrollbackする |
| rate limit | 既存の429として処理する | actionを実行しない |
| 予期しない失敗 | 内部情報を含まないgeneric message | commit前ならtransaction全体をrollbackする |

DB error objectは、unique violationのdetailにメールアドレスを含み得るため、そのままlogへ渡さない。
duplicate判定に必要なSQLSTATEなど、機密値を含まないallowlist済みの分類情報だけを扱う。

requestがcommitした後にnetwork responseだけ失われる可能性は、DB transactionでは除去できない。
管理者が送信前に認証情報を控える契約により、結果が不明な場合も同じ認証情報を失わない。
同じメールアドレスで再送して重複となった場合は、別browser sessionで控えた認証情報のloginを
確認してから共有する。

### Operator CLI

| 状態 | CLIの扱い | 永続化 |
|---|---|---|
| `AUTH_RETENTION_DATABASE_URL`未設定 | 接続前にerrorを返して非zero終了する | query・更新を行わない |
| 対象ユーザーなし | 対象なしを通知して非zero終了する | roleを変更しない |
| 対象が既に目的role | no-opを通知してzero終了する | roleを変更しない |
| DB接続・権限・query失敗 | secretや内部接続情報を表示せず非zero終了する | updateをcommitしない |
| 正常な昇格 | 対象ユーザーのrole変更成功だけを通知する | 対象1件を`admin`へ更新する |
| 正常な`--demote` | 対象ユーザーのrole変更成功だけを通知する | 対象1件を`user`へ更新する |

CLIは`DATABASE_URL`または`MIGRATION_DATABASE_URL`が利用可能でも代替接続として使用しない。
認証保守用接続の未設定や失敗を、より強い権限または異なる責務の接続で隠さない。

## Implementation Plan

| 対象 | 変更 |
|---|---|
| `backend/scripts/promote_admin.py` | 認証保守用接続だけで既存ユーザーを昇格・降格し、未設定・対象なし・DB失敗をfail-closedに扱うoperator CLIへ修正する |
| operator CLI tests | 接続選択、未設定時の無変更、対象なし、idempotency、昇格・降格、engine cleanup、secret非表示を検証する |
| `frontend/src/lib/auth/auth-config.ts` | runtime / Better Auth schema CLI / provisioning schemaが共有するclient-safeな8〜128文字のpassword policyを定義する |
| auth password module | `better-auth/crypto`の`hashPassword` / `verifyPassword`だけをserver-only moduleからre-exportし、独自hasherを実装しない |
| auth DB module | 現在`auth.ts`内にあるruntime用Pool生成をserver-only moduleへ移し、auth runtimeとtransaction serviceで共有する |
| provisioning schema | 名前、メールアドレス、passwordだけを受け付けるstrict Zod schemaを追加する |
| provisioning service | Better Auth標準password hash、JavaScript側の既存`uuid` v14、parameterized SQL、commit / rollback / releaseを担う専用transactionを追加する |
| admin Server Action | `requireAdminForAction()`、server validation、provisioning transaction、error mappingを実装する |
| admin user page | `/admin/users/new`へ日本語の3項目form、認証情報確認、pending、success / error feedbackを実装する |
| `/settings` | `デモユーザーを登録`導線を追加する |
| tests | authorization、schema、transaction、log redaction、form、route、実sign-in、E2E契約を追加する |

実装時は、公開登録停止PRがmainへ反映済みであることを確認してから専用branchを作る。今回と
無関係な未追跡specや利用者の変更には触れない。

## Verification

### Operator bootstrap

1. `AUTH_RETENTION_DATABASE_URL`未設定ではengine生成・DB接続・queryを行わず非zero終了する。
2. `DATABASE_URL`と`MIGRATION_DATABASE_URL`が設定済みでも、CLIの代替接続として使用しない。
3. 認証保守用接続で既存`user`を指定すると、対象1件だけを`admin`へ昇格できる。
4. 存在しないメールアドレスではユーザーを作成せず、roleを変更しない。
5. 既に`admin`の対象へ再実行しても成功扱いのno-opとなる。
6. `--demote`では既存対象1件だけを`user`へ戻し、再実行はno-opとなる。
7. 成功、no-op、対象なし、DB失敗の全経路で専用engineをdisposeする。
8. DB接続、権限、query、commit失敗時に対象roleを変更せず、接続URLやcredentialを出力しない。
9. 通常application用DB roleがauth userをUPDATEできない既存の権限分離を維持する。
10. UI、Server Action、HTTP route、schedulerからCLIを起動できず、別のadmin昇格経路が存在しない。

### Authorization / integration

1. 未認証requestとrole=`user`のrequestはuser / accountを作成しない。
2. admin sessionで正常な3項目を送るとuserとcredential accountを1件ずつ作成し、roleが`user`になる。
3. `role: "admin"`、`data.role: "admin"`、任意のuser ID、provider IDを送ってもuserを作成しない。
4. `/api/auth/admin/create-user`、`/api/auth/admin/set-role`を含む`/api/auth/admin/*`が404のままである。
5. 作成した認証情報で`POST /api/auth/sign-in/email`が成功する。
6. 発行時に新規ユーザー用sessionを作成せず、操作したadmin sessionを維持する。
7. public signupが引き続き`EMAIL_PASSWORD_SIGN_UP_DISABLED`で拒否される。
8. runtime、Better Auth schema CLI、provisioning schemaのpassword長境界が共有policyに一致する。
9. runtimeの`emailAndPassword.password`へcustom hash / verifyを設定せず、変更前から存在するcredentialで実Better Auth sign-inを維持する。
10. provisioningが`better-auth/crypto`からre-exportした`hashPassword`を使い、発行したcredentialで実Better Auth sign-inに成功する。

### Transaction / data integrity

1. password hash失敗時はDB queryを実行しない。
2. user insertを故意に失敗させた場合、user / accountを残さない。
3. account insertを故意に失敗させた場合、先にinsertしたuserもrollbackし、同じemailで再試行できる。
4. 同じメールアドレスの逐次・並行requestでuser / accountを各1件より多く作成しない。
5. commit失敗、rollback経路、例外経路でもPool clientを必ずreleaseする。
6. user / accountのcolumn名、PostgreSQL型、必須fieldがBetter Auth 1.6.23の生成schemaと一致する。
7. 実DBの`auth."user".email`にunique constraintが存在する。
8. user IDとaccount IDがJavaScript側の`uuid` v14 `v7()`で別々に生成され、SQL parameterとして渡される。
9. provider=`credential`、`accountId=userId`、user role=`user`、`emailVerified=false`が保存される。
10. DB、response、例外、server / client logにplaintext passwordを残さず、logにhashと利用者メールアドレスを出さない。

### UI / action

1. 非adminは`/admin/users/new`を表示できない。
2. pageに名前、メールアドレス、passwordだけを表示し、role fieldを表示しない。
3. `認証情報をコピー`は現在のemail / passwordだけをclipboardへ書き、logやstorageへ書かない。
4. `認証情報を控えました`が未確認の間は登録できず、email / password変更時に確認状態が解除される。
5. 送信中に入力値を保持し、各inputとbuttonを無効化して`登録中…`を表示する。
6. validation / transaction失敗後に入力値を保持し、passwordをerror messageやlogへ含めない。
7. 成功時に一般ユーザーとしての登録完了とemailだけを表示し、formを空にする。
8. `/settings`の導線から登録pageへ到達できる。

### Regression

1. 既存admin sign-in、通常user sign-in、session取得を維持する。
2. 既存のadmin route guardとrole narrowingを維持する。
3. Backend CLI test、Biome、TypeScript、frontend unit / integration test、production build、E2Eを通す。
4. package manifest、lockfile、Alembic revisionに変更がない。
5. Better Authの解決versionまたはlockfile変更時にschema、unique constraint、既存credential、新規credentialの互換testを実行する。

### Deployment smoke test

1. 認証保守用secretへアクセスできる一時的なoperator実行環境で、既存の自分用ユーザーをCLIから`admin`へ昇格する。
2. CLI終了後に一時的なoperator実行環境を破棄し、認証保守用secretがfrontendや通常application用DB接続へ混入していないことを確認する。既存のretention workerによる利用は維持する。
3. 昇格したユーザーでproductionへログインし、admin pageへアクセスできることを確認する。
4. `/admin/users/new`で認証情報を控えてからテスト用の通常ユーザーを1件発行する。
5. admin sessionが維持され、管理者画面へ引き続きアクセスできることを確認する。
6. 別browser sessionで発行した認証情報を使ってログインできることを確認する。
7. 発行したuserがadmin pageへアクセスできないことを確認する。
8. `/auth/register`とsignup APIが引き続き公開登録を受け付けないことを確認する。
9. `/api/auth/admin/create-user`と`/api/auth/admin/set-role`が404であることを確認する。

## Rollback

障害時は、operator CLI、ユーザー発行page、Server Action、transaction service、settings導線を
含まない直前のrevisionへ戻す。DB schema変更はないためmigration rollbackは不要である。

CLI codeのrollbackだけでは、既に昇格済みのroleを自動で戻さない。初期adminのroleを戻す必要が
ある場合だけ、修正版CLIの`--demote`をoperatorが明示的に実行する。application障害を理由に
自動降格しない。

発行処理が成功してcommit済みのuser / accountは、rollback後も通常ユーザーとして残る。
application rollbackを理由に既存アカウントを自動削除しない。発行済みアカウントの削除は
今回のscope外であり、必要になった場合は別仕様として扱う。

## Done

- 既存の自分用ユーザーを、認証保守用接続だけを使うoperator CLIで初期adminへ昇格できる。
- 認証保守用接続の未設定、対象ユーザーなし、DB失敗では何も変更せず失敗し、別DB接続へfallbackしない。
- UI、Server Action、HTTP APIからadminを作成・昇格できず、存在しない初期ユーザーをCLIで作成しない。
- 公開signupを再開せず、既存adminだけがデモ用一般ユーザーを発行できる。
- UIとServer Actionで未認証・非adminを拒否し、新しい管理HTTP APIを公開しない。
- role / data / ID注入でもadminを作成・昇格できない。
- userとcredential accountが1 transactionで作成され、途中失敗で孤児recordを残さない。
- 発行されたrole=`user`のアカウントが、控えたemail/passwordでログインできる。
- 独自hasherを追加せず、Better Auth標準password実装で既存credentialと発行credentialのsign-in互換性を維持する。
- JavaScript側の既存`uuid` v14でIDを生成し、実DBのemail unique constraintを競合防止の根拠として検証する。
- 初期adminへの明示的な昇格を除き、既存ユーザー、既存role、既存session、既存quotaを変更しない。
- 招待メール、期限、password変更、ユーザー管理機能を追加しない。
- plaintext passwordをresponse、URL、log、永続的なapplication stateへ残さない。
- Admin Plugin、DB migration、新規dependencyを追加しない。
- automated verificationとdeployment smoke testを実行できる。
- 実装完了時にStatusを`Implemented`へ更新できる。
