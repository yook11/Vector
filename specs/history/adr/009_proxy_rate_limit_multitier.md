# ADR-009: Frontend proxy rate limit を request-class × identity の multi-tier 化

> 日付: 2026-06 / ステータス: Accepted (Red-first 実装済)
>
> 実装の SSoT は `frontend/src/proxy.ts` / `lib/proxy/rate-limit-plan.ts` (tier 写像) /
> `lib/proxy/identifier.ts` (IP 解決) / `lib/auth/rate-limit.ts` (Redis 実行層)。
> 本 ADR は「なぜこの形にしたか」を記録する。
> 本 ADR は **ADR-006 の §1 (識別子・上限) と §4 (unknown bucket) を supersede**
> する。ADR-006 §2 (cookieCache 無効) / §3 (storage fail-open) と ADR-007
> (login limiter DB-backed) は据え置き。

## Context

通常の画面操作 — リロード数回 + Weekly Trends 閲覧 + Briefing 確認 — で
`429 Too Many Requests` が返る、という症状が報告された。

原因は **rate limit の対象範囲が広すぎる** ことにある。現行 proxy は静的
アセット以外の全 request を単一 `rl:ip:<ip>` 60 req/min で数える
(`matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"]`)。つまり
page GET・client-side RSC ナビゲーション・`<Link>` prefetch・`/api/*` が
**同じ財布**を消費する。

Next.js App Router は本番で viewport 内の全 `<Link>` を自動 prefetch し、
各 prefetch は origin への実 RSC GET になる。Vector の news dashboard は
`NewsCard` の `/news/[id]` を `per_page` 既定 24 (最大 100) 同時マウントする
ので、1 ロードで ~30-45 request、数回の操作で 60/min を容易に超える。
backend の weekly-trends / briefing には user-facing な 429 が無く、symptom の
429 は frontend proxy 起因で確定している。

外部仕様の調査で以下を確定し、修正設計の制約とした:

1. **prefetch は本番のみ・viewport 進入で発火**。大量リンクには `prefetch={false}`
   が公式推奨。(Next.js Prefetching)
2. **Next.js は RSC 内部ヘッダ (`rsc` / `next-router-prefetch` /
   `next-router-state-tree`) を proxy が見る前に strip する**。matcher の
   `missing: next-router-prefetch` 除外は機能しない (#63728)。proxy 関数内で
   信頼できる prefetch/RSC 信号は URL の `_rsc` query param のみ (#91723)。
   (Next.js Proxy)
3. **RSC リクエストに 429 を Response で返すとナビゲーションが壊れる** (#82790)。
   よって prefetch/RSC を「弾く」素朴な対応は逆効果。
4. **Fly は public HTTP service に `Fly-Client-IP` を付与し、`X-Forwarded-For`
   は spoof 注意**とされる (Fly Request Headers / Services)。production で
   `Fly-Client-IP` 欠如は通常ユーザーの identity ではなく **経路異常**。
5. **cookieCache 有効化は revoked session / role 変更が maxAge 秒間 stale に
   なる** (Better Auth)。Vector は DAL (`requireSession` →
   `auth.api.getSession`) が毎回 fresh 検証する設計 (ADR-006 §2 / #707) のため
   有効化できない。
6. OWASP API4 は endpoint の business need ごとに上限を調整すべきとし、
   Cloudflare も API / POST action を別観点で制限する例を出す。単一 60/min は
   auth には緩すぎ、prefetch には厳しすぎる。

### 現行コードと ADR-006 §1 の乖離

ADR-006 §1 は本来 **session-hash + anon-IP の 2 名前空間** (`rl:auth:<hash>`
120/min / `rl:anon:<ip>` 60/min) を決めていたが、PR10 の Fly-Client-IP 切替時に
「認証状態ごとの緩和は後段に任せる」と **IP-only 60/min に退化**した。さらに
元 §1 の設計には穴があった — 認証済みは `rl:auth:<hash>` のみで **IP backstop が
無く**、`getSessionCookie` は署名検証しない生パーサ (`better-auth/cookies`) の
ため、cookie 値を回せば 120/min を無限に突破できた。IP-only 化が偶然それを塞ぎ、
本 ADR の two-tier-AND が正しく塞ぎ直す。

## Decision (要約)

proxy の rate limit を **request class (`_rsc` GET / read / mutation) ×
identity (session / IP / unknown)** の multi-tier に再構成する。該当する全 tier を
満たせば allow、1 つでも超過で block する。`_rsc` は寛容な ceiling で別 tier 化し、
prefetch fan-out を誤検知しない。未解決 IP は identity ではなく異常として class 別に
扱う (read = fail-open、anon write のみ bounded + alert)。cookieCache 無効・storage
fail-open は維持する。

method 分類は **read = `GET` / `HEAD` / `OPTIONS` (safe・idempotent)** /
**mutation = `POST` / `PUT` / `PATCH` / `DELETE`**。`_rsc` 判定は `GET` 厳密一致のみ。

| request class | session 有 | session 無 |
|---|---|---|
| `_rsc` GET / IP 解決 | `rl:rsc:<ip>` 600 | `rl:rsc:<ip>` 600 |
| `_rsc` GET / IP 未解決 | **fail-open** | **fail-open** |
| read (GET/HEAD/OPTIONS) / IP 解決 | `rl:sess:<h>` 60 + `rl:ip:<ip>` 300 | `rl:ip:<ip>` 300 |
| read / IP 未解決 | `rl:sess:<h>` 60 | **fail-open** |
| mutation (POST/PUT/PATCH/DELETE) / IP 解決 | `rl:sess:<h>` 60 + `rl:ip:<ip>` 300 | `rl:ip:<ip>` 300 |
| mutation / IP 未解決 | `rl:sess:<h>` 60 | `rl:uwrite:global` 30 + **alert** |
| `/api/auth/*` | 上記 + **Better Auth DB limiter (主役)** | 同左 |

read と mutation の差は **「IP 未解決 かつ session 無」の終端のみ** (read=fail-open /
mutation=`rl:uwrite:global`)。HEAD/OPTIONS を read に含めるのは、IP 未解決の health
check / 監視 / CORS preflight が `rl:uwrite:global` を消費して false alert / 429 を
起こさないようにするため。

`<h>` = `sha256(session_token)` の先頭 16 文字 (ADR-006 §1 の convention を踏襲)。
上限はすべて env override 可: `RATE_LIMIT_RSC_PER_MIN=600` /
`RATE_LIMIT_SESSION_PER_MIN=60` / `RATE_LIMIT_IP_PER_MIN=300` /
`RATE_LIMIT_UNKNOWN_WRITE_PER_MIN=30`。

## 設計判断

### 1. prefetch/RSC は `_rsc` query param で判定し、寛容 ceiling で別 tier 化する (全 skip しない)

prefetch を除外する信号は strip される (#63728) ため、matcher の `missing` でも
request ヘッダでも判定できない。proxy 関数内で唯一信頼できるのは URL の `_rsc`
query param なので、`GET && searchParams.has("_rsc")` を prefetch/RSC とみなす。

**全 skip にはしない**。理由は ADR-006 が塞いだ **C8** (anon→1 user 登録→認証
cookie で大量 RSC リクエスト→各 RSC が `auth.api.getSession` で DB hit (cookieCache
無効)→pg.Pool 枯渇) が再オープンするため。`getSessionCookie` が署名検証しない以上
「認証済み RSC は安全」とは言えない。F12 (pg.Pool config: max=20 /
connectionTimeoutMillis=5000) で「全停止」→「一時的 5xx 自己回復」には格下げ済みだが、
それでも認証済み攻撃者が RSC フラッドで可用性を落とせる穴になる。

そこで `_rsc` GET は **寛容な ceiling (`RATE_LIMIT_RSC_PER_MIN` 既定 600, IP-keyed)**
で別管理する。通常 prefetch (1 ロード ~30-90、NAT 集約でも数百) は当たらず、持続
フラッド (数千/min) だけ bound する。研究結論「exempt **または** generously bucket」
のうち、C8 を閉じたまま誤 429 を消すため generously-bucket 側を採る。ceiling 超過時の
429 は当該ナビを壊す (#82790) が、寛容な閾値を超えるのは攻撃者だけ。

### 2. 認証 identity は未検証 session cookie 単独で信用しない (session tier は必ず IP で backstop)

per-user キーは NAT/CGNAT/社内回線で複数ユーザーが 1 IP を共有する誤 429 を解く。
だが `getSessionCookie` は署名検証しないので、**session cookie 値を単独の rate-limit
key にすると偽造 cookie を回してバケット無限生成 = 制限バイパス**になる。

そこで session tier (`rl:sess:<h>` 60/min) は per-user 公平性のために置くが、
**全 request は IP tier (`rl:ip:<ip>` 300/min) でも backstop** する (two-tier-AND)。
外部経路では IP tier が常に効くので、cookie を回しても総量は IP ceiling で頭打ち。
これが ADR-006 §1 の forge-bypass を構造的に塞ぐ。

真の userId を安価かつ検証付きで得る手段 — cookieCache 有効化 — は採らない
(ADR-006 §2 / 制約 5: stale authz = #707 回帰)。proxy での DB session 検証も採らない
(hot path に DB 往復が増え、proxy が auth DB に依存し、Next.js/Better Auth が middleware
での DB 検証を非推奨)。代償として **本方式は真の per-user 分離ではなく session-cookie
単位**であり、超大規模 NAT は最終的に IP ceiling 律速になる (保証範囲)。

### 3. 未解決 IP は identity でなく「観測すべき異常状態」— class 別に扱う

production では `Fly-Client-IP` が必ず付与される (制約 4) ので、欠如は経路異常
(internal health-check / flycast 内部 / edge bypass)。現行のように全部を単一
`rl:ip:unknown` に集約すると、(i) 正常な集約トラフィックだけで枯渇し IP 解決できない
全員を 429 にする、(ii) 1 経路が焼くだけで他を巻き込む、という **共有障害点**になる。
これは rate limit 実装で最も一般的なセキュリティギャップ。

未解決 IP はクライアントが操作できる入力ではない (edge が必ず header を付け、除去は
edge バイパス内部経路でのみ可能) ため、防御を緩めても攻撃面が開かない。よって
**read (GET/HEAD/OPTIONS) は fail-open** (カウントせず通す) し、共有 unknown bucket
由来の誤 429 を根絶する。これは現状の health-probe が `rl:ip:unknown` を焼く実害も
同時に消す。HEAD/OPTIONS を read 側に含めるのは、IP 未解決の health check / 監視 /
CORS preflight を mutation 終端 (`rl:uwrite:global`) に流し込んで誤 alert を出さない
ため。

mutation (POST/PUT/PATCH/DELETE) は最低限の入口防御を残すため、**session があれば
session tier で縛り** (authed write は pool しない)、**session も無い anon write のみ**
`rl:uwrite:global` (30/min) で bound する。この global バケットは anomaly 経路専用で
低ボリュームなので poison 耐性より「最低限の上限 + 異常の可視化」を優先する。

未解決 IP / fail-open は throttled な構造化ログ信号
(`frontend_rate_limit_missing_ip` / `frontend_rate_limit_unknown_write`、`server-log`
の warn-level event) を production のみ出す (dev は Fly Edge 非経由で IP 未解決が
常態のため抑制)。**コードが出すのは信号まで**で、実際の alert 発報は ops 配線
(Logfire / Fly metrics) に委ねる。production で unknown が定常化したら edge/topology
異常の運用シグナルとして扱う。

### 4. multi-tier の判定は atomic — deny 時はどの bucket にも書かない

複数 tier の sliding window log を 1 Lua eval で評価する。全 tier を先に ZCARD し、
**1 つでも上限以上なら deny して**どの bucket にも ZADD しない。全通過なら全 bucket に
ZADD + EXPIRE する。deny した request が他 tier の budget を消費すると窓が歪むため。
`Retry-After` は窓幅 (60s) 固定 (現行同様)。

## 却下した代替

| 案 | 却下理由 |
|----|---------|
| ADR-006 §1 の session-hash のみ (IP backstop 無し) | 署名検証なしの cookie を回して bypass (判断 2) |
| cookieCache 有効化で安価に userId 検証 | revoked/降格が maxAge 秒 stale 化し DAL 認可が腐る (#707 回帰 / 制約 5) |
| proxy で `auth.api.getSession` (DB session 検証) | hot path に DB 往復・auth DB 依存・middleware DB は非推奨 |
| `_rsc` を全 skip | C8 (RSC フラッド→Pool 枯渇) が再オープン (判断 1) |
| 未解決 IP を単一 `rl:ip:unknown` に集約 (現状) | 共有障害点・誤 429・poison (判断 3) |
| `RATE_LIMIT_PER_MIN` を上げるだけ | 根本 (単一財布 + prefetch カウント + IP only + unknown 集約) 未解決。auth budget も過大化 |

## 保証範囲 (過大表現しない)

- **best-effort / 真の per-user ではない**。session tier は (検証なしの) session-cookie
  単位で、IP tier で backstop した複合。超大規模 NAT は IP ceiling 律速で、完全な
  per-user 分離が要れば proxy DB 検証 (却下案) が必要になる。
- C8 は **閉じたまま**だが、`_rsc` ceiling (判断 1) + F12 pool config の二段で、
  RSC ceiling は持続フラッドのみを bound する (瞬間 burst は pool が fail-fast で吸収)。
- 上限値は prod 未実測。env で調整する前提 (大規模共有 egress が IP ceiling に当たるなら
  `RATE_LIMIT_IP_PER_MIN` を上げる)。
- alert は **コードが throttled 信号を出すまで**。発報の配線は ops scope。
- prefetch 抑制 (下記 follow-up) は client 側の load 最適化であって **server tier の
  代替ではない** (攻撃者は無視できる)。

## Consequences

- 通常閲覧の誤 429 が解消する。prefetch は寛容 ceiling 下、RSC ナビゲーションも同様、
  full-doc GET と Server Action は session 60 + IP 300 で tight に縛られる。
- redis-rl の key namespace が `rl:ip:*` / `rl:sess:*` / `rl:rsc:*` /
  `rl:uwrite:global` に拡張する。全キー TTL 持ちなので ADR-007 の `volatile-ttl`
  eviction は引き続き正しい。
- env 4 本追加 (`RATE_LIMIT_RSC/SESSION/IP/UNKNOWN_WRITE_PER_MIN`)。既存
  `RATE_LIMIT_PER_MIN` は deprecate し `.env.example` で移行を明記する。
- 読者は「同一 request が複数 tier を消費しうる」「`_rsc` は別財布」という非対称を
  理解する必要がある。proxy.ts / identifier.ts の docstring が補う。
- **follow-up (本 ADR scope 外)**: 高密度リンクの prefetch 抑制。`NewsCard` は
  hover/intent prefetch (`prefetch={active ? null : false}` + onMouseEnter) で
  origin/RSC レンダ負荷を削る。`Header` 等の少数リンクは default 据え置き。これは
  rate limit 挙動を変えない純粋な負荷最適化なので独立に進める。

## 関連

- **supersedes**: [ADR-006](006_better_auth_rate_limit_strategy.md) §1 (識別子・上限) /
  §4 (unknown bucket)。§2 (cookieCache 無効) / §3 (storage fail-open) は据え置き。
- [ADR-007](007_auth_ratelimit_db_storage.md) — 無変更。login limiter は DB-backed で
  別 failure domain。本 ADR は proxy 側 (Redis) の一般 limiter のみ再構成する。
- #707 — PPR static shell / DAL gate (cookieCache 無効維持の前提)。
- Sources: Next.js Prefetching / Proxy / #63728 / #91723 / #82790、Fly Request
  Headers / Services、Better Auth Session Management、OWASP API4、Cloudflare Rate
  Limiting Best Practices。
