# client IP 解決の信頼モード導入 (ALB 対応)

> 作成日: 2026-08-01
>
> Status: Approved (実装中)
>
> 対象: frontend の proxy / rate limit plan / Better Auth 設定 / SSE route

## Problem

production の client IP 解決は `Fly-Client-IP` ヘッダだけを信頼する。この前提は
「Fly edge が必ず上書きする」ことに依存していたが、AWS 移行で入口が ALB に変わり、
2 つの問題が生じた。

1. ALB は `Fly-Client-IP` を付けないため、production で IP が常に未解決になる。
   per-IP rate limit が消え、Better Auth の sign-in 制限 (60 秒 5 回) は全 client
   共有の単一バケツに fallback している (2026-07-31 に warn ログで実測)。
2. ALB は client が送った `Fly-Client-IP` を素通しで転送するため、攻撃者が任意の
   値を付けると最優先で信頼される。偽 IP でバケツを量産して IP rate limit を
   バイパスでき、Better Auth の IP 記録も汚染できる。

## Evidence

- `frontend/src/lib/proxy/identifier.ts` — 現行の解決順 (production は fly-client-ip のみ)
- `frontend/src/lib/proxy/rate-limit-plan.ts` — tier 分類と missing_ip / unknown_write 信号
- `frontend/src/lib/auth/auth.ts` — Better Auth `ipAddressHeaders` (production は fly-client-ip のみ)
- `frontend/src/app/api/research/runs/[runId]/events/route.ts` — SSE route が生ヘッダ 3 本を直接読む
- Better Auth (`@better-auth/core/utils/ip.mjs` 実体確認): `trustedProxies` 未設定時は
  **単一値ヘッダしか信頼しない** (カンマ区切り複数値は null)。
- Fly 公式 docs: X-Forwarded-For の**末尾はアプリ自身の shared/dedicated IP** であり
  client IP ではない。Fly では `Fly-Client-IP` が正。
- AWS ALB 公式 docs: `xff_header_processing.mode` = "append" (既定) は、既存の XFF が
  単一値でも複数値でも**末尾に**実測接続元を追記する。ALB 前段に他の proxy は無い
  (CloudFront 不採用、alb.tf 明記)。既定依存を避けるため alb.tf で明示 pin する。
- **未検証リスク**: client が `X-Forwarded-For` を**複数ヘッダ**で送った場合の ALB の
  正規化挙動は docs に記載が無い。ALB が 2 本目を素通しすると、Node の
  `Headers.get()` はカンマ結合するため末尾が client 制御値になりうる。緩和として
  解決値に IP 構文検証を課す (下記) が、完全な緩和ではない。本番で実測して
  この節を更新すること。
- 同 docs: `xff_client_port.enabled` を有効化すると追記値が `ip:port` 形式になる。
  現状は無効 (既定) で、有効化しても構文検証により fail-closed 側に倒れる。

つまり「XFF 末尾」は ALB では正だが Fly では誤りで、信頼構造はプラットフォーム固有。
推測ではなく明示フラグで宣言する (ecs.tf の `DB_IAM_AUTH` と同じ設計判断)。

## Design

### CLIENT_IP_TRUST (新 env)

production での信頼する出所を明示宣言する。

| 値 | 意味 |
|---|---|
| `alb-xff-last` | `x-forwarded-for` の末尾 1 値のみ信頼 (AWS)。`fly-client-ip` は読まない |
| `fly-client-ip` | `fly-client-ip` のみ信頼 (Fly、現行同等) |
| 未設定 / 不正値 | **fail-closed**: IP 未解決として扱い `missing_ip` 信号で可視化 |

dev / test はフラグを読まず現行 fallback (fly-client-ip → XFF 先頭 → x-real-ip) を維持する。

### 解決値の構文検証

解決した値は IPv4 / IPv6 として妥当な場合のみ採用し、それ以外は null (未解決) に倒す。
proxy は任意文字列を `rl:ip:<value>` に使えてしまう一方、Better Auth は `isValidIP` で
弾いて共有バケツへ落ちるため、検証が無いと「proxy 的には解決済みなので missing_ip が
出ないまま login limiter だけ黙って共有化する」観測不能な劣化が起きる。

### 識別単位の正規化 (IPv6 は /64)

rate limit の per-IP identity は「アドレス」ではなく「回線契約」を単位にする。
IPv6 は 1 契約に /64 が配られ、OS のプライバシー拡張が下位 64bit を頻繁に
取り替えるため、アドレスをそのままキーにするとローテーションだけでバケツを
無限分散でき、per-IP ceiling (forge-bypass backstop) が実効性を失う。

- IPv4: そのまま。
- IPv4-mapped IPv6 (`::ffff:a.b.c.d`): 埋め込まれた IPv4 に変換する。mapped 空間を
  /64 で丸めると全 IPv4-mapped client が 1 バケツに畳まれてしまうため、丸めの前に
  必ず剥がす。
- IPv6: /64 network 形に正規化する。先頭 4 hextet を実値、残り 4 hextet を 0 とし、
  全 8 hextet を 0 詰め 4 桁・小文字・`:` 区切りで展開した形にする (`::` 圧縮しない)。
  例: `2001:DB8::1` → `2001:0db8:0000:0000:0000:0000:0000:0000`。

full-form (非圧縮) を選ぶのは、`::` 圧縮形が zod の妥当性検証で端ケースに落ちうるのに対し
展開形は確実に妥当なため。

実装は再実装ではなく Better Auth の `normalizeIP` / `isValidIP`
(`@better-auth/core/utils/ip`) をそのまま流用する。再実装 + corpus ベースの contract test
は、corpus 外の入力クラス (大文字 `FFFF` の hex-mapped 形) で実装初日から oracle と
食い違っていたことがレビューで実証されたため採らない。同一実装の流用なら consumer 間
一致は構造的に成立し、上流の挙動変更も全 consumer へ同時に適用される。
`@better-auth/core` は direct dependency として better-auth と同版で exact pin する
(範囲指定だと npm が core だけ先行 patch に解決し、root と better-auth 配下で二重コピー
= 別実装に分裂する。1.6.25 で実発生を確認)。better-auth を bump する際は core も同版へ
揃えること。

consumer 間で identity が一致する load-bearing な根拠は、実は同一実装そのものではなく
**単一の内部ヘッダ `x-vector-client-ip` + Better Auth 側の冪等な再正規化**にある。Better Auth は
生 IP ではなく正規化済みヘッダを読み、その値は `normalizeIP` の不動点なので、re-normalize しても
変わらない。よって 3 consumer (proxy / Better Auth / SSE) は同一 /64 を指す。同一実装の流用は
defense-in-depth であり、仮に version split が起きても不動点性により consumer 間一致は保たれる。

正規化は解決時 (identifier) に行い、内部ヘッダにも正規化後の値を流す。全 consumer
(proxy rate limit / Better Auth / SSE) の識別単位を 1 箇所で揃えるため。帰結として
Better Auth の session.ipAddress 記録も /64 粒度になる (追跡は契約単位で成立する)。

### 内部ヘッダ `x-vector-client-ip`

- proxy.ts が解決済み IP を下流 (route handlers / Better Auth) へ渡す唯一の経路。
- proxy は外来の同名ヘッダを**必ず削除**し、解決できた場合のみ値を設定する。
  matcher が静的アセット以外の全 request を proxy に通すため、下流では無条件に信頼できる。
- Better Auth `ipAddressHeaders` は `["x-vector-client-ip"]` のみ (環境分岐を削除)。
  単一値なので Better Auth の single-value 信頼とそのまま噛み合う。
- SSE route は生ヘッダ 3 本の代わりにこのヘッダを読む。

### missing_ip 信号の health checker 除外

ALB health check は XFF 無しの GET で毎分 `missing_ip` warn を発火させ、信号を恒常
ノイズ化する。User-Agent が既知 health checker (`ELB-HealthChecker/` prefix) の
request は **`missing_ip` に限り**信号を出さない (rate limit 判定自体は従来どおり:
read は fail-open)。`unknown_write` は anon mutation flood の唯一の兆候であり、
UA は client が偽装できるため、抑制の対象にしない。

### 設定漏れと経路異常の区別

fail-closed を選んだ理由は「設定漏れで偽装穴が静かに残らない」ことなので、両者は
観測上も区別する。production で `CLIENT_IP_TRUST` が未宣言 / 不正値の場合、proxy は
プロセス毎 1 回 `frontend_client_ip_trust_unconfigured` warn (detail: unset / invalid、
生値は載せない) を出す。`missing_ip` は従来どおり「宣言済みだが edge が IP を
渡してこない」経路異常の信号として残る。

## Invariants

- production で、信頼できる出所により解決された IP 以外を rate limit identity に使わない。
- 非 ALB ingress (service connect 経由の backend→frontend 内部呼び出し等) は XFF を
  持たないため IP 未解決のままとする。既存どおり read は fail-open、anon mutation は
  `rl:uwrite:global` に入る (この経路に per-IP identity を発明しない)。
- `x-vector-client-ip` の consumer を proxy matcher の除外配下 (`_next/static` /
  `_next/image` / `favicon.ico` の前方一致) に置かない。除外配下は proxy を通らず
  外来ヘッダが素通しになるため、下流の無条件信頼が成立しない。
- `x-vector-client-ip` に外来値が残ったまま下流へ到達しない (proxy が常に削除または上書き)。
- dev / test の解決順・挙動は変更しない。
- 既存の tier 構造・limit 値・fail-open 分岐 (read / `_rsc` / uwrite) は変更しない。

## Non-goals

- Better Auth `trustedProxies` (CIDR) 方式の採用 (解決アルゴリズムを 2 系統にしない)。
- rate limit の上限値・バケツ設計の変更。
- infra 配布 (ecs.tf への `CLIENT_IP_TRUST=alb-xff-last` 追加は PR #90 merge 後に別途、
  Fly は次回 deploy 前に secret `CLIENT_IP_TRUST=fly-client-ip` を設定)。

## Done

- `CLIENT_IP_TRUST=alb-xff-last` で XFF 末尾が IP として解決され、`fly-client-ip` 偽装値が
  無視される。
- `CLIENT_IP_TRUST=fly-client-ip` で現行 Fly 挙動が保たれる。
- production で env 未設定なら IP 未解決 (fail-closed) + missing_ip 信号。
- Better Auth / SSE route / proxy rate limit の 3 消費者が全て同一の解決結果を使う。
- health checker UA の request からは missing_ip 信号が出ない。
- 上記が全てテストで保証され、`/check` が green。
