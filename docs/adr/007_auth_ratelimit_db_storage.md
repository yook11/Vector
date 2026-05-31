# ADR-007: Better Auth ログイン limiter を DB-backed 化 + redis-rl eviction policy 修正

> 日付: 2026-05 / ステータス: Accepted

## Context

frontend には 2 系統の rate limiter がある:

1. **proxy.ts の IP limiter** (`rl:ip:*`, sliding window log, Redis) — 一般トラフィック用。Redis 障害時 fail-open が正しい (ADR-006)
2. **Better Auth 内蔵のログイン limiter** (`/api/auth/*` 専用) — 認証 brute-force 防御

(2) は従来 Redis の `customStorage` (`auth.ts`) を使っていた。この customStorage は Redis エラーを握りつぶして `null` を返す実装で、Better Auth は `storage.get` を try/catch せず `null`=「前科なし=許可」と判定する (runtime `api/rate-limiter/index.mjs`)。結果として **Redis 障害時にログイン試行制限が無制限に fail-open する**穴があった。OWASP API2:2023 (Broken Authentication) は認証 brute-force 制限を fail-open させるなと明言しており、proxy 層の一般 IP limiter (fail-open が妥当) とは要求が異なる。

Better Auth は `rateLimit.storage: "database"` をネイティブ対応し、ログイン limiter のカウンターを **Postgres (既存 `auth` schema / pg.Pool)** に置ける。DB は Redis と別の failure domain で、Redis が落ちる場面でも生存している。これは ADR-006 の Alternative D (Sprint 4 送り) を DB-storage で実現するもの。

## Decision

### 1. ログイン limiter の storage を Redis → DB (`storage:"database"`) に変更

- 設定 (`enabled` / `storage:"database"` / `customRules`) を `frontend/src/lib/auth/auth-config.ts` に集約し、runtime (`auth.ts`) と migration (`auth.cli.ts`) で **import 共有**する。
- `storage:"database"` が migration 側にも無いと `better-auth migrate` が `auth.rateLimit` テーブルを生成しないため、両方が同一設定を見ることを構造で担保する。
- `customRules` は従来値を据え置き (挙動不変): `/sign-in/email` `/sign-up/email` = 60s/5 回、`/reset-password` = 60s/3 回。
- `auth.ts` の `rateLimitCustomStorage` と Redis client 依存は削除。

### 2. redis-rl の eviction policy を `allkeys-lru` → `volatile-ttl` に変更

- auth カウンターが redis-rl から消え、`rl:ip:*` (proxy IP limiter / 全キー 60s TTL) 専用になる。
- 全キーが TTL を持つため、残 TTL の短いものから捨てる `volatile-ttl` が正しい。
- `noeviction` は不可: 満杯時に write を拒否し、proxy 側の fail-open コードと相まって「満杯時に全体 bypass」を招く。

## 保証範囲 (過大表現しない)

- 全インスタンスが同一の共有 DB 行を見るため、in-process counter で起きる N× (machine 数倍) のゆるみは無い。
- ただし Better Auth の DB storage は `get → (判定) → set` の **read-modify-write であり atomic upsert / atomic increment ではない**。高並行下では lost update が起きうる。
- したがって本変更は「厳密な並行 brute-force 上限」ではなく **「共有 DB 上の best-effort limiter」**である。主目的は fail-open の穴を断つこと (∞ → 概ね 5/60s)。厳密上限が要るほどの脅威には CAPTCHA / account lockout (follow-up) を重ねる。

## 却下した代替

| 案 | 評価 |
|---|---|
| degrade (粗い全体上限 / in-process counter へ縮退) | 不採用 — DB storage が穴を構造的に塞ぐため degrade 機構自体が不要。コードも増える |
| CAPTCHA (Turnstile / hCaptcha) | この規模では過剰 (over-engineering)。将来 brute-force 実害が出たら follow-up |
| circuit breaker | 同上。別 failure domain への移設で代替できる複雑性 |

## 受容する残リスク

- `auth.rateLimit` テーブルは **自動掃除されない**。window 切れは in-place UPDATE で表現され、行は DELETE されない (GC 無し)。Redis TTL の自浄から永続 DB へ移したため、**IP ローテ / 分散攻撃時に行が無限に増え続ける**。
- 今回は prune を入れない (合意済)。これは「受容する残リスク」として明示する。
- **prune を追加するトリガ / 期限**: 次のいずれかで follow-up に着手する — (a) `auth.rateLimit` 行数が運用上無視できない水準 (目安 10 万行) を超えたとき、(b) production deploy 後に IP ローテ系の攻撃トラフィックを観測したとき、(c) 遅くとも production 初回 deploy を含む sprint 内。
- 将来の prune 実装: `DELETE FROM auth."rateLimit" WHERE "lastRequest" < <now_ms> - <window_ms>` (camelCase 列は要 quote、`lastRequest` は BIGINT epoch-ms)。scheduler の定期タスク等に追加する。

## 非対象 (follow-up)

- CAPTCHA (Turnstile)、account 単位 lockout、MFA (twoFactor plugin)、`auth.rateLimit` の定期 prune。
- proxy.ts の IP limiter (無変更・Redis + fail-open のまま正しい / ADR-006)。
- `customRules` の `/reset-password` が Better Auth の実エンドポイント (`/request-password-reset`) と不一致の可能性 — 今回は挙動不変のため verbatim 移設し、突き合わせは follow-up。

## マイグレーション

`auth` schema / Better Auth テーブルは Alembic 管理外で、better-auth CLI (`db-init-better-auth` が `vector_auth` ロールで実行) が管理する。`storage:"database"` を migration config に入れたことで `better-auth migrate` が `auth.rateLimit` (`key` unique / `count` / `lastRequest` BIGINT) を生成する。

- fresh volume: 4 段ブートストラップ (db-init-schema → db-init-better-auth → db-init-revoke-create → db-init-alembic) が自動生成。
- 既存 dev volume: `docker compose up --force-recreate db-init-schema db-init-better-auth db-init-revoke-create` で不足テーブルを追加。
- runtime grant: `n3_grant_app_db_users` が auth schema 全テーブルへ DML 付与済み + `rateLimit` は `vector_auth` 所有のため追加 grant 不要。

## Consequences

- Redis 障害時もログイン limiter が DB-backed で enforce され続け、fail-open の穴が構造的に消える。
- frontend のログイン経路は Redis 非依存になる (proxy IP limiter のみ Redis を使う)。
- DB 往復 (数 ms) が加わるが、ログインは人間操作速度なので無視可。pool は `statement_timeout:5000` で hang しない。
- ロールバックは customStorage 復帰で可能 (`auth.rateLimit` テーブルは残るが無害)。
