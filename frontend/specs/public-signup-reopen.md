# 公開登録の再開

> 作成日: 2026-09-29
>
> Status: Implemented
>
> 対象: frontend の Better Auth / 認証画面
>
> 前提仕様: `frontend/specs/invite-only-public-signup-shutdown.md`、`frontend/specs/admin-demo-user-provisioning.md`

## Problem

一定期間、AIエージェント機能を試せるように公開登録を再開したい。
現在は`disableSignUp: true`で公開登録を止めており、使えるのは管理者が手動で発行したアカウントだけである。
アカウントを受け取るまで試せないため、試してもらう機会を逃している。

停止時の仕様は、再開する場合はruntime flagではなく別PRで行い、登録方式・濫用対策・ユーザー作成上限・
メール確認の要否を改めて決めるとしている。本仕様でそれらを決める。

## Evidence

| 項目 | 確認内容 |
|---|---|
| Better Auth | 1.6.23。`emailAndPassword`は`enabled: true, disableSignUp: true`で、`auth.ts`と`auth.cli.ts`がobject単位で一致する |
| Signup endpoint | `POST /api/auth/sign-up/email`。emailは`z.email()`で検証して小文字化するが、trimはしない。nameは`z.string()`で空文字も通る。passwordは8〜128文字 |
| role | `additionalFields.role`は`input: false`。signupのbodyにroleがあっても既定値`user`で上書きされ、`/update-user`では`FIELD_NOT_ALLOWED`で拒否される |
| name | Better Authの標準定義で必須で、DBは`text NOT NULL`。画面表示にもbackendにも使っていない。UserMenuはemailを表示し、BFFのJWTはsubとroleだけを持つ |
| 重複email | 既定では422 `USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL`を返す。`autoSignIn: false`か`requireEmailVerification`を有効にすると、登録済みかどうかを区別できない応答になる |
| メール送信 | 送信手段はない。`emailVerified`は全ユーザー`false`で、参照している箇所もない |
| 登録の速度制限 | Better Authの`/sign-up/email`はIPあたり60秒に5回で、DBに保存する。本番は`CLIENT_IP_TRUST=alb-xff-last`でIPを解決する |
| LLM利用の上限 | researchは1ユーザーにつきJSTの1日10回まで。全体の上限はない |
| 費用 | Gemini / DeepSeekは前払い制。AgentCore web searchはAWS課金で、1 runは最大9クエリ（$0.063）、1ユーザー1日で最大$0.63。AgentCoreだけを対象にした日次予算$3の通知を設定済み（#500） |
| 他ユーザーのデータ | ユーザー所有のデータはすべてJWTのsubで絞っており、IDORは見つかっていない |
| 登録UI | `/auth/register`は招待制の案内ページ。ログイン画面には招待制のAlertがあり、登録リンクはない。旧`RegisterForm`は#41で削除済み |

## Decisions

### 1. 登録方式

- email / passwordのセルフサービス登録を再開する。`disableSignUp: false`を`auth.ts`と`auth.cli.ts`の両方に明示し、object単位の一致を保つ。
- フォームの入力はメールアドレスとパスワードだけにする。nameは空文字`""`で保存し、表示名は導入しない。
- メールアドレスは画面側でtrimし、形式を検証してから小文字化する（管理者発行と同じ規則）。サーバーはtrimしないため、画面側のtrimを省かない。
- パスワードは共有のpassword policy（8〜128文字）に従う。確認欄は設けない。
- 登録に成功したら自動でログインし、`/`へ移動する。
- 管理者によるアカウント発行は残す。

### 2. 濫用対策

新しい仕組みは足さず、既存の次の3つで抑える。

- 登録の速度: IPあたり60秒に5回。
- LLMの利用: 1ユーザー1日10回のresearch枠。
- 費用の監視: AgentCoreの日次予算$3（通知だけで、利用は止めない）。

CAPTCHAは採用しない。期間を区切った公開であり、上の上限と通知で費用を把握できるため。

### 3. ユーザー作成上限

設けない。アカウントを増やしても、使えるのは1アカウントにつき1日10回までである。費用は前払い残高とAgentCoreの予算通知で監視する。

### 4. メール確認

要求しない。送信手段がなく、`+`付きアドレスや使い捨てアドレスを使えば大量登録を防げないため、効果が薄い。本人確認をしないので、他人のアドレスでも登録できることは受け入れる。

### 5. 登録済みアドレスの判別

登録済みかどうかを区別できる応答（422）をそのまま使い、画面では「このメールアドレスは登録済みです。」と表示する。
利用者は本人と面接官などに限られ、利用している事実が知られても困る情報ではないため。
ログインの失敗理由を一律にする挙動は変えない。

### 6. 費用の上限と残るリスク

- 1ユーザー1日の外部検索費用は最大$0.63で、全体の上限はない。
- 予算の通知は費用データの反映を待つため数時間遅れ、利用も止めない。
- 濫用を検知したら、Rollbackの手順で公開登録を止める。

## Invariants

1. 公開登録で作られるユーザーのroleは常に`user`であり、登録のbodyからroleや`emailVerified`を指定できない。
2. 公開登録のnameは`""`で保存する。
3. 保存するメールアドレスはtrimと小文字化を済ませたもので、既存の一意制約で重複を防ぐ。
4. 登録に成功すると、user・credential account・sessionを作り、ログイン状態になる。
5. 既存ユーザーのログインと、発行済みのセッションは変わらない。
6. 管理者によるアカウント発行と、adminの認可境界は変わらない。
7. 画面はsecurity boundaryとして扱わない。登録できるかどうかの正本は、Better Authのサーバー設定とする。
8. `auth.ts`と`auth.cli.ts`の`emailAndPassword`はobject単位で一致する。

## Non-goals

- メール確認、パスワード再設定、メール送信。
- CAPTCHAなどのbot対策。
- 表示名の導入と、メールアドレス表示の見直し。
- ユーザー作成数と、全体のrun数の上限。
- 登録済みアドレスの判別の防止。
- 退会とアカウント削除。

## Implementation Plan

| 対象 | 変更 |
|---|---|
| `lib/auth/auth.ts` / `auth.cli.ts` | `disableSignUp: false`にする |
| `lib/auth/auth-client.ts` | `signUp`をエクスポートする |
| `features/auth/schemas/auth.ts` | 新規ユーザー作成で共通のemail / password schemaを切り出し、`RegisterSchema`を追加する |
| `features/auth/components/RegisterForm.tsx` | 新規。メールアドレスとパスワードの登録フォーム |
| `app/auth/register/page.tsx` | 案内ページを登録フォームに置き換える |
| `features/auth/components/LoginForm.tsx` | 招待制の案内を外し、登録へのリンクを足す |
| テスト | 設定・統合・フォーム・ページ・e2eを、登録できる前提に置き換える |
| `README.md` | 招待制の記述を直す |

## Verification

### Automated

1. 実際のBetter Auth handlerで、登録するとuser・account・sessionが作られ、session cookieが返る。
2. bodyの`role: "admin"`と`emailVerified: true`は保存されない。
3. nameは`""`で、メールアドレスは小文字で保存される。
4. 重複したメールアドレスは422 `USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL`になり、レコードは増えない。
5. 許可していないoriginからの登録ではレコードが作られない。
6. admin用のendpointは404のまま。
7. 既存ユーザーがログインできる。
8. 登録フォームの入力検証、エラー表示、送信中の表示、成功時の遷移。
9. ログイン画面に登録リンクがある。
10. Biome、TypeScript、frontend test、production buildが通る。

### Deployment smoke test

1. `/auth/register`に登録フォームが表示される。
2. 自分が管理するメールアドレスで登録すると、ログイン状態で`/`に移動し、researchを送れる。
3. ログイン画面に登録リンクがある。
4. 既存のメールアドレスで登録すると「登録済みです」と表示される。

## Rollback

公開登録を止めるときは、`disableSignUp: true`に戻すPRを出してrolloutし、画面とテストも招待制に戻す。
runtime flagは使わない。作成済みのアカウントとセッションは残る。

## Done

- 公開登録で`user`ロールのアカウントを作れ、登録後すぐにログイン状態になる。
- 登録画面・ログイン画面・READMEが公開登録の運用と一致する。
- 既存ユーザー、管理者によるアカウント発行、adminの認可境界が変わらない。
- automated verificationを通過し、deployment smoke testを実行できる。
- 実装完了時にStatusを`Implemented`へ更新できる。
