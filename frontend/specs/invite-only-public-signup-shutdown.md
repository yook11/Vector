# 招待制アクセスと公開登録停止

> 作成日: 2026-07-21
>
> Status: Implemented
>
> 対象: frontend の Better Auth / 認証画面 / デプロイ設定

## Problem

Vector は現在ポートフォリオとして運用しており、不特定多数の利用者を受け付ける必要がない。
一方、現在の Better Auth と `/auth/register` は公開登録を許可しているため、第三者が新しい
アカウントを作成できる。

Turnstile による bot 対策は実装途中だが、これは登録の自動化を難しくするだけで、公開登録
自体を停止するものではない。利用形態を招待制へ変更する以上、CAPTCHA を維持するより、
登録境界をサーバー側で閉じる方が要件に直接一致する。

## Evidence

| 項目 | 確認内容 |
|---|---|
| Better Auth | `emailAndPassword.enabled` は `true` で、`disableSignUp` は未設定 |
| Signup API | `POST /api/auth/sign-up/email` が公開登録を受け付ける |
| Signup UI | `/auth/register` が `RegisterForm` を表示する |
| Login UI | ログイン画面から `/auth/register` への `Register` リンクがある |
| Turnstile | widget、captcha plugin、CSP、環境変数、deploy preflight の変更はすべて未コミット |
| Admin UI | `/settings`、`/admin/pipeline-status`、`/admin/source-health` は存在する |
| User management | 管理者によるユーザー作成、招待、一覧、無効化、ロール変更画面は存在しない |
| Admin plugin | Better Auth Admin Plugin は server / client / CLI のいずれにも未導入 |
| Existing operation | `promote_admin.py` は既存ユーザーのロール変更だけを行い、ユーザーは作成しない |
| Canonical origin | 現在の正式な公開 origin は `https://vector-frontend-yook1.fly.dev` |

Better Auth 1.6.23 の `emailAndPassword.disableSignUp` は、email/password の signup を
無効化する公式設定である。endpointはhandlerより前にbody schemaで`name`の文字列、`email`の
形式、`password`が空でない文字列であることを検証し、origin middlewareも通過させる。
その後、handlerはpasswordの最小・最大長を検証するより前に`disableSignUp`を判定する。

したがって、body schemaとorigin middlewareを通過したrequestはuser作成処理より前に
`400` / `EMAIL_PASSWORD_SIGN_UP_DISABLED`で拒否される。body schema不正または不正originは
別のstatus / codeで先に拒否され得るが、どの拒否経路でもuser / account / sessionを
作成しないことを共通の契約とする。Better Auth内部の検証順序をこれ以上広いresponse契約には
しない。

一次情報:

- [Better Auth Options](https://better-auth.com/docs/reference/options)
- [Better Auth Email & Password](https://better-auth.com/docs/authentication/email-password)
- [Better Auth Admin Plugin](https://better-auth.com/docs/plugins/admin)

## Decisions

### 1. 公開登録をサーバー側で停止する

runtime の Better Auth 設定で次を有効にする。

```ts
emailAndPassword: {
  enabled: true,
  disableSignUp: true,
  minPasswordLength: 8,
}
```

`enabled: true` は既存ユーザーの email/password sign-in を維持するため残す。
`disableSignUp: true` により、画面を迂回して `POST /api/auth/sign-up/email` を直接呼ばれても
新しい user / account / session を作成しない。

runtime と CLI 用 Better Auth 設定の drift を避けるため、`auth.ts` と `auth.cli.ts` の
`emailAndPassword` 設定はobject単位で同じ値にする。`disableSignUp`自体はDB schemaへ影響
しないためmigrationは不要だが、`auth.cli.ts`のdocstringには認証モードもobject単位で同期
する意図を一文で補う。

### 2. 登録ページは招待制の案内ページとして残す

`/auth/register` は削除や 404 にせず、ブックマークや既存リンクから到達した利用者へ方針を
説明するページとして残す。公開登録フォーム、入力欄、登録ボタンは表示しない。

表示文言は日本語UIへ合わせて次を基準とする。

```text
招待制で運用しています
現在、一般向けの新規登録は受け付けていません。
アカウントをお持ちの方はログインしてください。
```

ログイン画面へ戻る `ログイン` 導線を1つ設ける。client state を必要としないため、案内は
Server Component として実装する。

### 3. ログイン画面から登録導線を外す

ログイン画面の公開登録リンクを削除する。代わりに招待制であることと、一般向けの新規登録を
受け付けていないことをフォーム上部へ日本語で明示し、存在しないセルフサービス登録へ誘導しない。

認証処理、成功時の遷移、credentialの内訳を隠すエラー契約は変更しない。表示文言とvalidation
messageは日本語へ統一する。送信中は入力値を保持したまま入力欄とボタンを無効化し、spinnerと
`ログイン中…`を表示して二重操作を防ぐ。認証失敗後も入力値を保持する。

### 4. Turnstile は採用しない

公開 signup を受け付けないため、signup CAPTCHA は不要である。現在の未コミット差分から
次を取り除き、公開登録停止PRへ含めない。

- Turnstile widget と signup token transport。
- Better Auth captcha plugin と Siteverify 設定。
- `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY`。
- Docker / Compose / Fly / CI の Turnstile 設定と secret preflight。
- Cloudflare 用の CSP `script-src` / `frame-src` 許可。
- Turnstile 固有の unit / integration test。
- Turnstile を前提とした旧仕様書。

Turnstile変更はコミット履歴に存在しないため、削除専用PRや revert commit は作成しない。
実装開始時に `git diff` と `git status` で確認したTurnstile対象のpath / hunkだけを選択的に
取り除き、公開登録停止に必要な差分だけを新たに作る。作業ツリー全体へのreset / cleanや、
今回と無関係な追跡・未追跡ファイルの削除は行わない。

### 5. 管理者画面の責務は変更しない

既存の管理者画面と `admin` ロールの認可境界は維持する。今回、次の機能は追加しない。

- 管理者によるユーザー作成。
- 招待メールまたは招待token。
- ユーザー一覧、検索、停止、削除。
- パスワードの発行または再発行。
- Better Auth Admin Plugin。

したがって、公開登録停止後に利用できるのは、停止前からDBに存在するアカウントだけである。
新しい招待ユーザーの登録方法は別仕様で扱う。

### 6. 正式ドメインを固定する

現在の canonical public origin は次とする。

```text
https://vector-frontend-yook1.fly.dev
```

production の `BETTER_AUTH_URL` は、末尾スラッシュ、path、query、fragmentを付けず、この値と
完全に一致させる。`trustedOrigins` は既存どおり `BETTER_AUTH_URL` を正本として利用する。

このfrontend URLはポートフォリオの公開導線なので、公開repositoryへ記載できる情報として
扱う。Public Repository Hygieneでは公開frontend URLだけを例外とし、backend / workerの
app名、internal hostname、secret、非公開の運用手順は引き続きcommitしない。

本番設定値は既存どおりGitHub Environment secretの`BETTER_AUTH_URL`からdeployする。
今回、canonical originとの完全一致または追加の形式検証を行うCI preflightは追加せず、
既存の非空確認を維持する。

Fly が提供する既定ドメインを利用し、今回 custom domain、DNS、TLS certificate、複数origin
対応は追加しない。将来ドメインを変更する場合は `BETTER_AUTH_URL` と公開入口を同時に
切り替え、既存利用者の再ログインを許容する。

## Invariants

1. `POST /api/auth/sign-up/email` は入力内容にかかわらず公開ユーザーを作成しない。
2. signup 拒否時に `user`、`account`、`session` のレコードを追加しない。
3. 既存ユーザーの `POST /api/auth/sign-in/email` は従来どおり利用できる。
4. 同じDBと`BETTER_AUTH_SECRET`で発行済みの既存sessionを無効化しない。
5. `admin` ロールと管理者画面の認可境界を変更しない。
6. `/auth/register` に form、email/password input、signup buttonを表示しない。
7. ログイン画面に公開登録へのリンクを表示しない。
8. ログイン送信中と認証失敗後に、入力済みのemail/passwordを画面上で保持する。
9. client-side UI は登録停止のsecurity boundaryとして扱わない。
10. signup停止の正本は Better Auth のserver-side設定とする。
11. production の canonical public origin と`BETTER_AUTH_URL`は
   `https://vector-frontend-yook1.fly.dev` で一致する。
12. Turnstile key、script、iframe、server-side verificationを残さない。

## Non-goals

- 管理者またはCLIからのユーザー作成。
- 招待の発行、承認、有効期限、再送、取消。
- Better Auth Admin Pluginの導入。
- DB schema / migrationの変更。
- dependencyの追加。
- 既存ユーザー、session、roleの変更または削除。
- sign-in、password reset、sign-outへのCAPTCHA追加。
- 日次run制限、IP制限、複数アカウント検知の変更。
- body schema不正または不正originに対するstatus / error codeの統一。
- custom domainまたは複数hostname対応。
- AWS移行構成の設計。

## PR Strategy

Turnstile実装は未コミットなので、最初に「Turnstileを削除するPR」は作らない。

公開登録停止を1つのPRとして作り、基準ブランチとの差分を次に限定する。

1. Better Auth のserver-side signup停止。
2. 招待制の案内ページ。
3. ログイン画面からの登録導線削除。
4. signup拒否とsign-in維持のテスト。
5. canonical public originの設定契約と公開repository方針の例外。
6. 本仕様書。

Turnstile差分を一度コミットしてから削除すると、採用しない実装の履歴とレビューコストだけが
増えるため行わない。

## Implementation Plan

仕様承認後、次を変更する。

| 対象 | 変更 |
|---|---|
| `frontend/src/lib/auth/auth.ts` | `disableSignUp: true` を設定し、未コミットのcaptcha plugin差分を除く |
| `frontend/src/lib/auth/auth.cli.ts` | runtimeと同じ`emailAndPassword`設定にし、schema非依存の認証モードもobject単位で同期する意図をdocstringへ補う |
| `frontend/src/app/auth/register/page.tsx` | フォームを招待制案内へ置き換える |
| `frontend/src/features/auth/components/LoginForm.tsx` | 日本語の招待制案内と、入力値を保持する明確なログイン中表示を追加する |
| `frontend/src/features/auth/components/LoginForm.test.tsx` | 登録リンクなし、招待制表示、pendingと認証失敗時の入力保持を固定する |
| signup integration test | signup拒否、DB非作成、既存ユーザーのsign-in成功を実handlerで固定する |
| `frontend/e2e/register.spec.ts` | 登録成功テストを招待制案内の確認へ置き換える |
| public signup専用コード | `RegisterForm`、そのtest、未使用になる登録schemaを削除する |
| Turnstile未コミット差分 | config、widget、test、CSP、env、deploy変更を作業ツリーから除く |
| `CLAUDE.md` / `README.md` | ポートフォリオの公開frontend URLだけをPublic Repository Hygieneの例外として明記する |

実装時に、今回と無関係な未追跡ファイルや利用者の変更には触れない。

## Verification

### Automated

1. body schemaとorigin middlewareを通過する代表的なsignup requestを実Better Auth handlerへ
   送り、`400`と`EMAIL_PASSWORD_SIGN_UP_DISABLED`を確認する。
2. body schema不正、不正origin、正常形式の各拒否後に、memory adapterのuser / account /
   sessionが追加されていないことを確認し、不正requestのstatus / codeは固定しない。
3. 事前作成した既存ユーザーがemail/passwordでsign-inできることを確認する。
4. 同じDBと`BETTER_AUTH_SECRET`を使い、変更前設定で発行したsessionが変更後設定の
   `get-session`で受理されることを確認する。
5. `/auth/register` の案内にform、textbox、`Create account` buttonがないことを確認する。
6. ログイン画面に`/auth/register`リンクがないことを確認する。
7. ログイン送信中のspinner、`ログイン中…`、操作無効化と入力値保持を確認する。
8. 認証失敗時に日本語のgeneric errorを表示し、入力値を保持することを確認する。
9. source tree、CSP、deploy設定にTurnstile参照が残っていないことを確認する。
10. Biome、TypeScript、frontend test、production buildを通す。

### Deployment smoke test

1. 変更前に既存の管理者アカウントでproductionへsign-inし、そのbrowser sessionを保持する。
2. `BETTER_AUTH_URL=https://vector-frontend-yook1.fly.dev` を確認する。
3. deploy後に`/auth/register`が招待制案内を表示することを確認する。
4. signup APIが新規ユーザーを作成せず拒否することを確認する。
5. deploy前から保持したsessionで`/settings`へアクセスできることを確認する。
6. sign-out後に同じ管理者アカウントで再度sign-inできることを確認する。

## Rollback

公開登録の再開はruntime flagによる一時切替にせず、要件変更として別PRで行う。
再開時は登録方式、濫用対策、ユーザー作成上限、メール確認の要否を改めて仕様化する。

障害時のrollbackは直前のapplication revisionへ戻す。DB schemaを変更しないため、database
rollbackは不要である。

## Done

- server-side signupが無効で、直接APIを呼ばれてもユーザーを作成しない。
- 既存ユーザーと管理者のsign-inが維持される。
- 登録ページとログインページが招待制の運用と一致する。
- 管理者ユーザー作成が今回のscope外であることが明記される。
- Turnstile未コミット差分がPRへ含まれない。
- canonical public originとGitHub Environmentの`BETTER_AUTH_URL`の運用契約が一致する。
- 公開frontend URLだけがPublic Repository Hygieneの例外として明記される。
- deploy前に発行済みの既存sessionと、既存ユーザーの新規sign-inが維持される。
- automated verificationを通過し、deployment smoke test手順が実行可能である。
- 実装完了時にStatusを`Implemented`へ更新できる。
