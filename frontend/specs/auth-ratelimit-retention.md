# auth.rateLimit retention 仕様

作成: 2026-07-07
状態: 実装済み

## Problem

Better Auth の DB-backed rate limiter は `auth."rateLimit"` に request
counter を保存するが、window 経過後も行を自動削除しない。通常時は同じ key が
in-place update されるだけだが、IP ローテーションや分散攻撃では key が増え続ける。

この仕様では、BFF 認証境界で使う Better Auth rate limit の一時 counter を
cron で定期削除し、認証 brute-force 防御を壊さずに保存期間を短く保つ。

## Evidence

| 項目 | 確認内容 |
|---|---|
| Better Auth 設定 | `frontend/src/lib/auth/auth-config.ts` で `rateLimit.storage = "database"`、custom rule は最大 `window: 60` 秒 |
| Better Auth schema | DB storage の `rateLimit` は `key` unique / `count` / `lastRequest`。`lastRequest` は epoch ms |
| Better Auth 挙動 | window 切れ時は行を削除せず、`count=1` / `lastRequest=now` に更新する |
| 既存 ADR | `specs/history/adr/007_auth_ratelimit_db_storage.md` は `auth.rateLimit` に GC が無く、将来 prune が必要と記録している |
| proxy rate limiter | `frontend/src/lib/auth/rate-limit.ts` の Redis sliding window は 60 秒 TTL 付きで自浄するため、本仕様の対象外 |
| 権限境界 | `vector_auth` は `auth.*` DML を持つ。`vector_app` は `auth.user` の SELECT/REFERENCES のみで、`auth."rateLimit"` を触らない |

外部根拠:

- Better Auth Rate Limit docs: database storage と `rateLimit` schema (`lastRequest` epoch ms)
- OWASP API2:2023 / NIST SP 800-63B: 認証 endpoint には brute-force / rate limiting が必要
- OWASP Logging Cheat Sheet / FTC / ICO storage limitation: 一時的・個人識別性を持ち得るデータは目的に必要な期間を超えて保持しない

## Invariants

1. `auth."rateLimit"` の保存目的は rate limit enforcement であり、監査ログではない。
2. 現行の enforcement window は最大 60 秒。削除対象は enforcement に不要な行だけにする。
3. `key` は IP と path に由来するため、不要になった行を長期保存しない。
4. Better Auth の table schema / modelName / runtime 挙動は変更しない。
5. `vector_app` に `auth."rateLimit"` 権限を広げない。purge は auth 境界の接続権限で行う。
6. purge 失敗でログイン処理や通常 request を止めない。失敗は log で可視化し、次回 cron に持ち越す。
7. 削除は小バッチで行い、長時間 lock や大きな write spike を作らない。
8. SQL は bound parameter を使い、`.env` を直接読まない。設定値は設定層経由で扱う。

## Retention Policy

### Schedule

cron は 30 分ごとに実行する。

実装時の cron literal は、既存 maintenance cron との衝突を避けるため
`20,50 * * * *` を採用する。

既存の重い maintenance は `:00/:30`, `:05/:35`, `:10/:40` に寄っており、
`pipeline_events` purge は `:25` である。`:20/:50` はそれらと直接重ならない。

### Cutoff

削除対象は `lastRequest` が実行時点から 10 分より古い行とする。

```text
cutoff_ms = now_epoch_ms - 10 * 60 * 1000
delete where auth."rateLimit"."lastRequest" < cutoff_ms
```

10 分は保存目的上の保持期間ではなく、60 秒 enforcement window に対する安全余白である。
cron が 30 分ごとに動くため、実際の通常滞留はおおむね 10〜40 分になる。

## Delete Semantics

削除は key-based batch delete とする。PostgreSQL は `DELETE ... LIMIT` を直接持たないため、
subquery で対象 key を絞る。

```sql
DELETE FROM auth."rateLimit"
WHERE "lastRequest" < :cutoff_ms
  AND "key" IN (
    SELECT "key"
    FROM auth."rateLimit"
    WHERE "lastRequest" < :cutoff_ms
    ORDER BY "lastRequest" ASC
    LIMIT :batch_size
  )
```

外側にも `"lastRequest" < :cutoff_ms` を置き、subquery 選択後に Better Auth が同じ行を
新しい request で更新した場合に、更新済み行を削除しにくくする。

推奨実行単位:

- `BATCH_SIZE = 1000`
- `MAX_BATCHES = 5`
- batch 間 sleep は `0.05〜0.1s`
- 1 回の cron で削り切れない場合は次回へ持ち越す

`lastRequest` index は初期実装では追加しない。DB schema 変更になるため、行数や実行時間を
見て必要になった時点で別途判断する。目安として、`auth."rateLimit"` が 10 万行を超える、
または purge が継続的に timeout へ近づく場合は index 追加を検討する。

## Execution Boundary

`auth."rateLimit"` は Better Auth が所有する `auth` schema の table である。
purge は `vector_auth` 相当の auth 接続権限で実行し、core backend の `vector_app`
権限を広げてはならない。

採用実装は backend maintenance worker に auth DB 用 session/connection を追加し、
`broker_maintenance` 上の cron task として実行する方式である。

検討した実装候補は 2 つある。

1. backend maintenance worker に auth DB 用 session/connection を追加し、
   `broker_maintenance` 上の cron task として実行する。
2. frontend/BFF 側の運用 cron から auth DB 接続で実行する。

どちらを選んでも、auth schema を触るための接続情報は設定層から注入する。
既存 `.env` の直接 read / 表示 / 編集はしない。

## Observability

purge task は少なくとも次を structured log に出す。

| event | 条件 | attributes |
|---|---|---|
| `auth_rate_limit_retention_disabled` | kill switch 無効時 | なし |
| `auth_rate_limit_retention_purged` | 正常終了時 | `deleted`, `batches`, `max_batches`, `cutoff_ms`, `retention_seconds` |
| `auth_rate_limit_retention_failed` | DB 接続 / SQL 失敗時 | error class/name。secret / DSN / key 値は出さない |

`key` 値は IP を含み得るため log に出さない。

## Configuration

実装で設定を追加する場合は設定層に閉じる。

| 設定 | 既定 | 目的 |
|---|---:|---|
| `auth_retention_database_url` | `None` | auth schema retention 用 DB 接続。env 名は `AUTH_RETENTION_DATABASE_URL` |
| `auth_rate_limit_retention_enabled` | `true` | kill switch |
| `auth_rate_limit_retention_max_batches` | `5` | 1 回の cron での DB 負荷上限 |

`AUTH_RETENTION_DATABASE_URL` は backend / SQLAlchemy asyncpg 用なので、
frontend / node-pg 用の既存 `AUTH_DATABASE_URL` とは分ける。retention 秒数 (`600`) と
cron literal (`20,50 * * * *`) は初期実装ではコード定数とし、運用で変更要求が出るまで
env 化しない。

## Non-goals

- CAPTCHA / account lockout / MFA の追加。
- Better Auth の rate limit rule (`window`, `max`, path) の変更。
- Better Auth table schema / modelName の変更。
- `auth."rateLimit"` への index 追加。
- `pipeline_events` retention の変更。
- proxy Redis rate limiter の変更。
- 長期監査・攻撃分析用の raw request history 保存。

攻撃傾向を長期に見る必要が出た場合は、`auth."rateLimit"` raw row を残すのではなく、
削除件数、429 件数、認証失敗件数などの aggregate metric で扱う。

## Test Plan

実装時は次を固定する。

1. 10 分より古い `auth."rateLimit"` 行だけ削除され、10 分以内の行は残る。
2. 削除対象が空の場合、`deleted=0`, `batches=0` で正常終了する。
3. `max_batches` と `batch_size` により 1 回の削除件数が上限で止まり、残りは次回へ持ち越される。
4. kill switch が false のとき DB delete を実行しない。
5. log に DSN / secret / `key` 値が出ない。
6. purge が使う接続権限は auth schema 用であり、`vector_app` への auth table DML 付与を要求しない。

## Done

- `auth."rateLimit"` retention の対象 table / schedule / cutoff / SQL 形が仕様化されている。
- cron 方針は「30 分ごと、10 分より古い行を小バッチ削除」で固定されている。
- auth schema の権限境界を壊さない実装方針が明記されている。
- 実装時のログ・設定・テスト観点が明記されている。
- Done を満たしたら、index 追加や CAPTCHA 等の周辺改善は別タスクとして扱う。
