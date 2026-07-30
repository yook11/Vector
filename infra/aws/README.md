# infra/aws — ネットワークと境界

Vector を AWS へ移す構成。ネットワーク / IAM / RDS / ElastiCache / ECS / ALB /
egress proxy を宣言する。bootstrap スタック (`bootstrap/`) が先に必要。

## この層が主張していること

権限を「設定で守る」から「構造で守る」に置き換える。道具は強さで 2 種類に分かれ、
それぞれの層を混ぜない。

| | 道具 | 決めること | 単位 |
|---|---|---|---|
| **不在** | public IP を持たない | 外から宛先に指定できるか | task |
| **不在** | ルートテーブルの `0.0.0.0/0` | VPC の外に出られるか | subnet |
| 設定 | security group | VPC の中で誰に届くか | SG 参照 |
| 設定 | egress proxy の allowlist | 外のどのホストに出られるか | subnet (送信元 IP) |
| 設定 | VPC endpoint policy | AWS サービスの何に触れるか | endpoint |

**上 2 つと下 3 つでは守っているものの種類が違う。** 設定による防御は何枚重ねても
「正しく書かれている限り」という条件が付き、退行しうる。存在しないものは退行しない。
さらに設定は攻撃対象領域を減らさない — パケットは届き、プロセスがそれをパースする。
アドレスの不在は届く前に止める。

**subnet が権限の単位になる。** proxy が識別できるのは送信元 IP だけで security
group は見えないため、allowlist を分けたい粒度で subnet を分ける必要がある。
subnet 自体は無料なので段ごとに 1:1 で切る。

## 守っている不変条件

- **インターネットにアドレスを持つのは AWS マネージドの ALB と NAT Gateway だけ。**
  自前コンポーネントは 1 つも public IP を持たない (`grep assign_public_ip *.tf` が
  全て `false`)。SG や Squid の src ACL は「入ってきたものを拒否する」防御で、
  正しく書かれている限りという条件が付く。アドレスの不在は「入ってこられない」
  防御で、設定の退行では戻らない。**ACL 評価はリクエストのパース後**なので、
  パーサ段の脆弱性は ACL では守れない点も差になる。
- **app subnet のルートテーブルに `0.0.0.0/0` が無い。** VPC の外へ出る経路を
  egress proxy 1 本に限定する。設定でうっかり外に出るのではなく、経路が存在しない。
  **強制力の担い手は NAT の不在ではなく、`rt-app` に経路が無いこと。** だから NAT は
  proxy の後ろに経路装置として置いてよい。引けるのは `rt-proxy` だけで、NAT Gateway
  自体もルートテーブル経由の転送しかしない (private IP を直接宛先に指定しても
  応答しない) ため、app から proxy を迂回する経路は生まれない。
- **frontend の外向き経路は ECR のレイヤー取得だけ。** Logfire も外部 API も
  呼ばないため proxy への接続を許さない。ただし image pull のために S3 への 443 は
  全段で開ける必要があり、SG だけでは「リージョンの S3 全域」になってしまう
  (public-writable bucket への PUT は credential 不要なので、task role が空でも
  exfil 経路になる)。**S3 Gateway endpoint の endpoint policy** で ECR のレイヤー
  bucket への `s3:GetObject` に絞り、入口を持つ唯一の段の外向きを read 1 つに戻す。
  この前提は Better Auth がメール送信も social provider も持たないこと (招待制、
  検証済み) に依存する。計測 SDK を足すと壊れるが、静かに漏れるのではなく
  接続失敗で明確に壊れる。
- **`VPC endpoint policy` が最後の 1 枚。** SG が「どのサービスに出られるか」までしか
  絞れないところで、endpoint policy が「そのサービスの何に触れるか」を絞る。
- **backend への到達は frontend からのみ。** transport 層の defense in depth で、
  application 層の `BFF_JWT_SIGNING_SECRET` 検証と合わせて 2 層。
- **security group は egress も明示する。** 規則を書かなければ全拒否になるので、
  必要な相手だけを列挙する。console で作った SG は既定で egress 全許可だが、
  Terraform の `aws_security_group` は作成時にその既定規則を剥がすため、
  standalone rule で書いた分だけが有効になる。
- **段に配るのは実際に使うものだけ。** `stages` の `db_users` / `needs_broker` /
  `egress_vendors` が SG 規則と IAM policy と squid.conf の生成元になる。
  例えば scheduler は cron を発火するだけで DB engine を作らない
  (`scheduler_entrypoint.py` が `is_scheduler_process=True` で `WORKER_STARTUP` を
  立てないため) ので、`db_users` が空になり RDS への到達も `rds-db:connect` も配られない。
  **重複する事実を 2 箇所に書かない** — 「DB を使うか」は `db_users` の長さから導く。
- **冗長化の水準を 1 箇所に合わせる。** RDS を Single-AZ にした時点で全体の可用性の
  下限は 1 AZ。ECS task も Valkey も同じ AZ に置き、cross-AZ 転送費をゼロにする。
  `az_secondary` は ALB と RDS subnet group の 2 AZ 要件を満たすためだけに存在する。

## 段の宣言は 1 箇所

`locals.tf` の `stages` が subnet と security group を生成する。棚卸しの表が
そのまま入っており、段を増やすときに触るのはここだけ。

## ハマりどころ

- **interface endpoint の ENI は 1 AZ につき 1 subnet にしか置けない。** 7 つの app
  subnet 全部には置けないので api の subnet に集約し、他からは VPC の local ルートで
  届かせる。到達制御は `sg-vpce` が行う。課金が「4 endpoint × 1 AZ」に収まる根拠。
- **app subnet にも S3 Gateway endpoint の経路が要る。** frontend も含めて全段。
  Fargate の image pull は ECR のレイヤーを S3 から取るため、これが無いと
  そもそも task が起動しない。**ルートだけでは足りず SG の egress も要る**
  (gateway endpoint の prefix list を参照する)。task が起動しないときの第一容疑者。
- **`ecs` / `ecs-agent` / `ecs-telemetry` の interface endpoint は不要。** EC2 launch
  type の ECS agent 用で Fargate task には要らない。ECS Exec を使うなら
  `ssmmessages` が 1 本増える。
- **`enable_dns_hostnames` は必須。** 無効だと interface endpoint の private DNS が
  効かず、ECR / SSM / Logs の名前が public IP に解決されて経路が無くなる。
- **`NO_PROXY` に内部 frontend 名を入れる。** worker-insights の revalidate 送信は
  `trust_env` 既定 True の httpx なので `HTTPS_PROXY` を拾う。proxy は private 宛先を
  拒否する設計なので、除外しないと内部通信が静かに失敗する。

## apply の前にやること

1. **`bootstrap/` を apply する** (state bucket / OIDC / CI ロール / boundary /
   service-linked role / hosted zone)。
2. **レジストラの NS をこの hosted zone に向ける。** 委任が伝播するまで ACM の
   DNS 検証が完了せず、ALB も作れない。
3. **SSM parameter に実値を入れる** (`aws ssm put-parameter --type SecureString`)。
   `aws_ssm_parameter` の `value` は computed で refresh のたびに state に載るため、
   箱ごと Terraform の管理外に置いている。path は `terraform output` で出る。

## 運用の帰結

- **初回 apply 直後は全 service が起動失敗ループになる。** このスタックが ECR repo を
  作るので、その時点では image が無い。正常系は **apply → push → 安定**。
- **`ignore_changes = [task_definition]` の代償。** app-deploy が revision を進め、
  Terraform はそれを巻き戻さない。帰結として **Terraform 側で env / secrets /
  サイズを変えても service は旧 revision のまま**動く (新 revision は作られるが
  反映されない)。infra 起因の変更は「apply 後に app-deploy を再実行」が正規手順。
- **Valkey の ACL ミスは二重に静か。** worker 側は taskiq が XGROUP CREATE の
  NOPERM を debug で握り潰し、後段の XREADGROUP が NOGROUP で落ちて初めて発現する。
  frontend 側は fail-open で rate limit が黙って無効化され、60 秒ごとの
  `frontend_rate_limit_redis_fail_open` warn しか出ない。apply 後は
  (a) worker ログに NOGROUP が無いこと、(b) この warn が無いこと、を明示的に見る。
- **SSE は ALB の idle timeout (既定 60 秒) を跨がない。** backend の
  `sse.py` が `heartbeat_interval = 10.0` 秒でハートビートを流すので 6 倍の余裕がある。
  **不変条件: keepalive 間隔 < ALB の idle timeout。** 片方を変えるならもう片方も見る。
- **frontend の health check が DB に依存している。** `/auth/login` の SSR 成功を
  条件にしているため、**RDS が落ちると frontend も ALB から外れて全断**する。
  Fly と同じ判断を引き継いだ既知の受容 (root は redirect するので健全性の指標に
  ならない)。cascade を避けたいなら DB に触らない専用 endpoint が要る。

## egress proxy の残余

- **オープンプロキシ化はアドレスの不在が防いでいる。** proxy は private subnet に
  public IP 無しで置く。仮に 3128 の inbound SG を CIDR で緩めても、インターネット
  からこの task を宛先に指定する手段が無い。SG (app SG 参照のみ) と squid.conf の
  `src` ACL + 末尾の `deny all` は、その内側に残る自前設定 2 枚。
- **外向きの送信元 IP は EIP で固定される。** `terraform output egress_public_ip`。
  proxy を再デプロイしても変わらないので、外部ベンダー側の allowlist に登録できる。
- **非公開レンジの正本は app 側の 1 ファイル** (`backend/app/shared/security/
  non_public_ranges.json`)。Terraform は `jsondecode(file(...))` で読んで
  squid.conf を生成し、app は実行時の判定に使う。**ポリシーの持ち主はアプリで、
  Squid は写し**。一致は `TestNonPublicRangeParity` が固定する。
  不変条件は等価ではなく **proxy の deny ⊆ app の deny**
  (app が先に落とせば `ProxyError` への誤分類が起きない)。
- **proxy が落ちると全 egress が止まり、Logfire も止まる。** 障害の観測は
  CloudWatch 側 (コンテナログは interface endpoint 経由で proxy を通らない) に
  置く。Logfire に寄せると循環する。

### parity 化で見つかった 3 件 (2026-07-28、修正済み)

| | 症状 | 原因 |
|---|---|---|
| `100.64.0.0/10` (CGN) | app が public 扱い | `is_private` / `is_global` / `is_reserved` がすべて False |
| `192.88.99.0/24` (6to4 relay、RFC 7526 廃止) | app が public 扱い | **`is_global=True`** |
| **`::ffff:100.64.0.1` 等の v4-mapped** | 上 2 件が v6 表記で素通り | 正本の v6 リストに `::ffff:0:0/96` を置いていないため、レンジ照合が v6 側だけを見ていた |

3 件目は「テストで挙動を pin する」つもりが**実在の穴**だった。
`https://[::ffff:100.64.0.1]/` のような URL literal で到達できた。
修正は埋め込み v4 の展開。6to4 と Teredo はレンジ全体が `is_private` なので
フラグ側が拾っており、同じ穴は無い。

**`not ip.is_global` への置換は採らない。** `100.64.0.0/10` は直るが
`192.88.99.0/24` は `is_global=True` のため直らない (実測)。

## 初回起動で落ちる候補 (切り分け用)

- **S3 への SG egress** — route があっても SG で落ちて image pull が失敗する
- **service-linked role** — bootstrap で作成済み (ECS / ALB / RDS / ElastiCache)
- **CloudWatch Logs の Resource 形** — `awslogs` driver は既定 blocking なので、
  log stream を作れないと task が起動しない
- **interface endpoint の不足** — `ecs` / `ecs-agent` / `ecs-telemetry` は Fargate では
  不要だが、`ssmmessages` は ECS Exec を使うなら要る
- **RDS の CA bundle** — `rds.force_ssl` + `verify-full` で繋ぐので、backend の image と
  frontend の `pool-ssl.ts` の両方が RDS の CA (`rds-ca-rsa2048-g1` 系) を信頼して
  いる必要がある。**Neon の CA とは別物**。「接続はできるのに証明書検証で落ちる」枠
- **GRANT との突き合わせ** — IAM が決めるのは入口だけ。`fetch` が `vector_collect`
  だけで dispatch と collection の両方を賄えるか、`analysis` の maintenance が
  `vector_app` で purge を全部できるかは、provisioning 時に migration の GRANT と
  1 回突き合わせる。IAM が緩くても GRANT 側で落ちるだけなので事故にはならないが、
  切り分けが速くなる

## 未決 / 未検証

- **S3 endpoint policy の bucket が足りるか。** `prod-<region>-starport-layer-bucket`
  以外に必要な bucket が無いかは実測で 403 を見て確かめる。
- **agent 側の `ProxyError` 分類。** `tavily.py` の `except httpx.RequestError` は
  collection 用の写像を通らないため、allowlist の設定ミスが run report 上
  `status="provider_failed"` (= Tavily 障害) に化ける。
- **proxy image を ECR に置く CI。** repository は Terraform が作るが image は無い。
  proxy が起動しないと全 egress が止まるので、初回は apply → proxy push → app push。

## Terraform の外にあるもの

- SSM parameter の値 — CLI
- app 側の変更 (backend / frontend 両方の IAM トークン生成 / ガードの接尾辞 /
  proxy の明示注入 / pool 縮小)。node-redis は password が無いと AUTH を省略して
  default user で繋ぐため、frontend の実装が入るまで rate limit は fail-open で
  静かに無効のまま

## 使い方

```
terraform init
terraform plan
```

`plan` には AWS 認証情報が要る。構文と参照の検証だけなら
`terraform init -backend=false && terraform validate`。

state は S3 + ネイティブロック (`use_lockfile`)。bucket 名は公開 repo に置かないため
`-backend-config` で渡す:

```
terraform init -backend-config="bucket=$(cd bootstrap && terraform output -raw state_bucket_name)"
```

CI の `plan` は **`-lock=false`** で走らせる (lock オブジェクトの書き込みが
read-only の plan ロールでは通らない)。
