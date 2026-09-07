# Fly.io 時代の構成ファイル (退役済み)

2026-08-01 に Fly.io + Neon PostgreSQL での運用を終了し、本番は AWS (`infra/aws/`) 単独に移行しました。
ここにあるのは Fly 時代の構成例です。**現在どこからも参照されておらず、デプロイにも使われていません。**
削除せず、当時どういう境界でアプリを分割していたかの記録として残しています。

## 移動対応表

| 移動前 (Fly 運用時のパス) | 現在のパス |
|---|---|
| `backend/fly.core.toml` | `docs/legacy/fly/backend-core.fly.toml` |
| `backend/fly.collect.toml` | `docs/legacy/fly/backend-collect.fly.toml` |
| `frontend/fly.toml` | `docs/legacy/fly/frontend.fly.toml` |
| `infra/redis/fly.toml` | `docs/legacy/fly/redis-broker.fly.toml` |
| `infra/redis-rl/fly.toml` | `docs/legacy/fly/redis-rate-limit.fly.toml` |

各ファイル内のコメントにある自己パス表記 (`# infra/redis/fly.toml — vector-redis` など) や
`fly deploy -c infra/redis/fly.toml` といったコマンドは、**移動前のパス**を指しています。
当時の記録としてそのまま残しているため、現在のパスへの読み替えは上の表で行ってください。

## 当時の構成 (Fly 5 app / 全て nrt リージョン)

秘密の重さと攻撃面の広さで app を分割し、公開入口を frontend 1 つに絞る構成でした。

### `vector-core` — [backend-core.fly.toml](backend-core.fly.toml)

api / worker-analysis / worker-insights / worker-agent / scheduler の 5 process group を
同一 image から起動する crown-jewel 側。AI 鍵・BFF 鍵・revalidate 鍵を保持します。
`[http_service]` を宣言せず public IP も持たない internal-only 構成で、flycast (org-private DNS)
経由でのみ到達できます。「frontend からしか backend に届かない」という認証境界を、
アプリケーション層の BFF 鍵検証だけでなくネットワーク層でも担保するためです。

### `vector-collect` — [backend-collect.fly.toml](backend-collect.fly.toml)

外部 web を fetch し untrusted HTML を parse する worker-fetch 専用。
RCE / SSRF の攻撃面を持つため、AI 鍵も本物の BFF 鍵も持たせず秘密を最小化して core から分離しました。
inbound を一切持たず (`[[services]]` 非宣言)、Redis Stream を pull して外部へ egress するだけです。

### `vector-frontend` — [frontend.fly.toml](frontend.fly.toml)

Next.js BFF / Better Auth / proxy。システム唯一の公開入口です。

### `vector-redis` — [redis-broker.fly.toml](redis-broker.fly.toml)

core / collect が共有する Taskiq broker。`noeviction` + AOF + volume で、
broker message を evict も restart で消失もさせません。
Redis ACL で core (全権) と collect (自分が触る Stream のみ) を分離し、
collect 侵害時の blast radius を自 Stream に限定していました。

### `vector-redis-rl` — [redis-rate-limit.fly.toml](redis-rate-limit.fly.toml)

frontend の `proxy.ts` が使う IP sliding-window rate limit 専用。
broker とは信頼境界・揮発性・eviction 方針が真逆なので別 app に分けています。
`volatile-ttl` / 永続化なし / singleton (カウンタが in-memory のため複数 Machine 化すると window が割れる)。

## 既知の負債

`backend/specs/redis-production-topology.md` が broker Redis の SSoT として `redis-broker.fly.toml` を指し、
契約テスト (`backend/tests/test_queue_separation_operator_contract.py`、
`backend/tests/queue/test_collect_acl_integration.py`) もこのファイルを読んでいます。
実体は `infra/aws/valkey.tf` に移っているため、次フェーズで契約の参照先を移す必要があります。

## 補足

app 名や URL は公開リポジトリ衛生の方針どおり placeholder のままです
(実 production の app 名は commit しません)。
