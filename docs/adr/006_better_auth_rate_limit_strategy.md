# ADR-006: Frontend rate limit / cookieCache の戦略

> 日付: 2026-05 / ステータス: Proposed

## Context

Red-team セッション `20260501T224446Z` で、**anon→1 user 登録のみで再現可能な Critical chain (C8)** が発見された。

### C8 の連鎖構造 (4 構造欠陥)

```
[anon]
  ↓ (AUTH-L3) minPasswordLength=8 — 1 user 取得は容易
[1 user 認証済 cookie 取得]
  ↓ (F17) requireSession が auth.api.getSession({headers}) 直呼び
       → Better Auth router の rate limit を完全 bypass
[認証済 cookie で大量 RSC リクエスト]
  ↓ (F16) findSession は user JOIN 1 クエリ DB hit
  ↓ React.cache が同 request 内重複は集約するが、request 跨ぎは無防備
  ↓ (cookieCache 無効) 全リクエストが DB hit に直行
[(F12) pg.Pool max=10 / connectionTimeoutMillis=0]
  ↓ Pool 全枯渇、後続リクエストは無限待機
[frontend 全停止]
```

参照: `/Users/you/Vector/.red-team/20260501T224446Z/report.md:41-56`

### Better Auth 仕様の前提

公式ドキュメント / GitHub issue 調査により以下が確定:

1. **Better Auth 内蔵の rate limit (`betterAuth({ rateLimit })`) は `/api/auth/*` HTTP router にしか効かない** — Server Component / Server Action が `auth.api.getSession({ headers })` を直接呼ぶ Vector のパターンには **完全にバイパスされる**
   出典: https://better-auth.com/docs/concepts/rate-limit
2. **cookieCache 有効化は Next.js Server Component で同期問題が複数報告されている** — `disableCookieCache: true` を渡しても update が伝搬しない既知 issue あり
   出典: https://github.com/better-auth/better-auth/issues/7008, #4389
3. **revoke / role 変更は cookieCache 有効時に最大 maxAge 秒間 stale な session が通る**
   出典: https://github.com/better-auth/better-auth/issues/4512

### 構造的に解決できる欠陥は 2 つ

| 欠陥 | 解決策 | 構造的有効性 |
|---|---|---|
| F17 (rate limit bypass) | proxy.ts に application-level rate limit 投入 | **構造的解決** — "1 認証 cookie あたりの req/min" を bound |
| F12 (Pool 設定なし) | pg.Pool config 明示 (別 PR) | 防御深化 — 単独では C8 解決不可だが "無限待機" を "fail-fast 5xx" に格下げ |

AUTH-L3 (password 8 桁) は credential stuffing 対策として独立議論対象であり、C8 構造の "認証済 1 user で N requests" 非対称性は解決しない。本 ADR スコープ外。

## Alternatives

| 案 | 概要 | 評価 |
|---|---|---|
| A: proxy.ts に application-level rate limit (Redis-backed) | チョークポイント最上流で throttle | **採用** |
| B: getCurrentSession 内に process-level rate limit | session 取得 helper 内で throttle | 不採用 — Server Action や proxy 経路でも別途 hook が必要、複雑 |
| C: cookieCache 有効化で DB hit 量を削る | Better Auth secondary cache で根本解決 | 不採用 — Server Component 経路で互換性問題 (#7008)、stale role による認可漏れ |
| D: Better Auth secondary-storage 設定で built-in rate limit を強化 | `/api/auth/*` のみ rate limit 強化 | 別 PR (Sprint 4) — `auth.api` 直呼び経路は依然 bypass される |

## Decision

### 1. proxy.ts に Redis-backed sliding window rate limit を投入

- **投入箇所**: `frontend/src/proxy.ts` 先頭。CSP nonce 生成・session 検証より前に reject する
- **識別子**:
  - 認証済 (Better Auth session cookie 存在): cookie 値の SHA-256 hash 先頭 16 文字
  - 匿名: `x-forwarded-for` 第一値 → fallback `x-real-ip`
  - 別 namespace で独立 throttle (`rl:auth:<hash>` / `rl:anon:<ip>`)
- **アルゴリズム**: sliding window log (Redis ZSET + Lua script で 1 round trip atomic)
- **上限値** (env で override 可能):
  - 認証済: `RATE_LIMIT_AUTHED_PER_MIN` (default 120)
  - 匿名: `RATE_LIMIT_ANON_PER_MIN` (default 60)
- **Runtime**: Next.js 16 の proxy は Node.js runtime 固定のため、追加設定不要 (node-redis / node:crypto を素で使える)
- **Storage**: Redis db index 1 (backend は db 0、frontend は db 1 で論理分離)

### 2. cookieCache は **意図的に無効** 維持

- 現状の `session: { cookieCache: { enabled: false } }` (`frontend/src/lib/auth/auth.ts:34-37`) を維持
- 理由:
  - admin 降格 / session revoke の即時反映が認可上必須 (現状の Vector の運用ポリシー)
  - Better Auth + Next.js Server Component の組合せで cookieCache 同期 bug が複数報告 (#7008, #4389)
  - DB hit 量の削減は **rate limit + pg.Pool 設定 (別 PR)** で代替する

### 3. Redis 障害時はフェイルオープン

- Redis 接続不可・eval 失敗時は `{ allowed: true }` を返して通す (rate limit を skip)
- 理由: rate limit は "DoS 防御の二次防衛線"。Redis 障害が全リクエスト 503 に直結すると運用障害が DoS に等価になる
- 一次防衛線は **pg.Pool 設定 (別 PR の `connectionTimeoutMillis: 5000`)** に委ねる
- 接続失敗・eval 失敗は `console.warn` で 1 回だけ記録 (将来 Sentry / logfire 連携)

## Consequences

### 良い影響

- C8 の核心 (F17) を構造的に塞ぐ。1 認証 cookie あたり 120 req/min を上限に bound するため、PR-D (pg.Pool 設定) と組み合わせて Pool 飽和攻撃が成立しない
- cookieCache 無効を維持するため、admin 降格 / session revoke は引き続き次リクエストで即時反映 (UX / セキュリティ要件を変更しない)
- Better Auth official rate limit と独立した経路なので、将来 Better Auth secondary-storage を追加しても干渉しない

### 制約 / トレードオフ

- frontend container が Redis に依存 (`REDIS_URL` 必須)。Redis 障害時はフェイルオープンするが、運用監視で接続失敗 warn を拾う必要がある
- IP 識別子は reverse proxy / Fly Edge の信頼に依存。docker-compose 直接公開の現状では `x-forwarded-for` を client が詐称可能 (per-IP throttle 回避)。Sprint 3 の reverse proxy 導入で `Fly-Client-IP` 等 trusted header に差し替え予定

### 将来の拡張余地

- 上限値は env で override 可能。prod 実測で調整可能 (.env で運用調整、code 変更不要)
- 将来 Fly.io 移行時は managed Redis (Upstash 等) に切替可能 (interface は node-redis に閉じる)
- `/api/auth/*` 自身への secondary-storage rate limit 強化は Sprint 4 で別途検討
