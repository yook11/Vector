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

## 通常のinfra変更

PRのplanを確認し、mainへmergeした後、`AWS terraform apply`を`production`で承認する。
infra変更のmain pushでは自動起動し、同じ内容の再実行はmainからの手動起動を使う。
ローカルの通常CI roleはplan / pushだけに限定し、apply / migration / rolloutは使わない。
bootstrapの継続更新は[専用ロールの手動手順](bootstrap-access/README.md)、初期構築・実行権限の変更・非常時復旧は管理者操作とし、通常の承認失敗を迂回しない。
切り替えと既発行sessionの扱いは[導入・運用手順](MIGRATION_WORKFLOW.md)を参照する。

## 初回構築のapply前にやること

1. **`bootstrap/` を apply する** (state bucket / OIDC / CI ロール / boundary /
   service-linked role / hosted zone)。
2. **レジストラの NS をこの hosted zone に向ける。** 委任が伝播するまで ACM の
   DNS 検証が完了せず、ALB も作れない。
3. **SSM parameter に実値を入れる** (`aws ssm put-parameter --type SecureString`)。
   `aws_ssm_parameter` の `value` は computed で refresh のたびに state に載るため、
   箱ごと Terraform の管理外に置いている。path は `terraform output` で出る。

## EmbeddingConsumer基盤の追加（スライス1）

[仕様書](../../specs/pipeline/embedding-consumer.md)に対応するSQS・DLQ・専用ネットワーク・IAM・ログ・通知を定義する。Lambda本体とSQSイベントソースマッピングはまだ作成しない。この変更だけでは受信は始まらず、Taskiqとrelayの稼働設定は維持される。

### 適用順序

1. PRで本体・bootstrapの差分と検証結果を確認する。bootstrapは管理者の既存経路で先に適用し、Consumer専用boundary・ロール作成許可・PassRole制約・DLQ管理権限を整える。本体CIロールでbootstrapを変更しない。
2. embedding元キューの保持期間を14日から4日に短縮する前に、滞留状況と元の投入時刻を確認する。4日以上のメッセージがある、または有無を判断できない場合は本体の適用を止め、扱いを確認する。purge・自動退避は行わない。`ApproximateAgeOfOldestMessage`だけでは繰り返し失敗したメッセージを見落とす可能性があり、古い滞留がないことを断定しない。調査のための受信もreceive countを増やすため、無造作に全件受信しない。
3. mainへのmerge後、既存の`AWS terraform apply`を承認して本体を適用する。既存relayのイメージdigestと状態は現行ワークフローで引き継ぎ、キューの再作成・想定外の削除がないplanを確認する。
4. proxyのallowlist更新はECS proxyサービスの更新を伴う。更新後に既存ワーカーの外部通信とproxyの稼働を確認する。ConsumerサブネットにはGeminiだけを許可する。
5. Consumerを有効化する前に、`embedding_consumer_parameter_path` outputのパスへ既存と同じGemini APIキーを登録する。既存の管理者手順でSecureString・AWS管理キー`alias/aws/ssm`を使用し、値を標準出力・シェル履歴・CIログ・Terraform変数へ出さない。パラメーターの作成・実値はTerraform管理外で、`.env`から取得しない。

### 接続・監視と後続作業

- サブネットはprimary AZのCIDR index 28で、既存のappルートテーブルに接続する。Consumer SGの送信先はRDS:5432、proxy:`proxy_port`、SSM endpoint:443だけ。
- SSMの専用SGは既存SSM endpointだけに追加する。新しいNAT・endpoint・Redis経路は作らない。後続のSSMクライアント設定では、SSMのprivate endpointを外向きHTTP proxyへ送らない。
- 元キューは保持4日・可視性720秒・受信上限5回、DLQは保持14日。StandardキューのDLQ保持期限は元の投入時刻を基準とし、元キューでの期限切れはDLQ移動ではなく削除となる。
- `embedding-consumer-dlq-not-empty`アラームはDLQの可視メッセージが1件以上のときに既存SNS経由で通知する。Maximum・60秒・1評価期間で判定し、欠測は正常扱い。ALARM/OKへの遷移を通知し、メッセージ単位の通知・繰り返し通知は行わない。OKは原因解消や処理成功の保証ではない。
- 自動停止と自動再投入は実装しない。障害が続く場合は後続で作るSQSトリガーを手動で無効化し、原因解消後に有効化する。既存backfill holdではLambdaを停止できない。
- DLQの手動再投入は対象・現在の記事状態を確認して行う。既存元キューの送信元endpoint制限を維持しているため、コンソールからのredriveがそのまま使えるとは扱わず、経路と操作権限を含む実行手順をスライス4で確定する。
- Lambdaのイメージ取得、関数管理権限、SSM取得コード、メモリ、SQSトリガーはスライス3で追加する。実接続・再配信・DLQ移動・通知配送は有効化時に検証する。

### AWSを変更しない検証

本体と`bootstrap/`それぞれで`terraform fmt -check`、`terraform init -backend=false -input=false -lockfile=readonly`、`terraform validate`、`terraform test`を実行する。モックテストはAWS providerを置き換え、架空のアカウント・ドメインを使用する。

既存のbackend初期化情報・tfvars・stateと混ぜないよう、検証にはTerraformソース・lockfile・templates・testsのみを一時ディレクトリへコピーする。proxyが参照する`backend/app/http/non_public_ranges.json`は相対配置を保つ。実環境のplanは既存のread-only経路で`-lock=false`を使い、SSMの値を取得しない。

## 運用の帰結

- **初回 apply 直後は全 service が起動失敗ループになる。** このスタックが ECR repo を
  作るので、その時点ではimageが無い。新規DBの初期構築は別作業で完了させ、
  **apply → image作成 → verify承認によるledger作成 → app反映承認** の順に進める。
- **`ignore_changes = [task_definition]` の代償。** rollout が revision を進め、
  Terraform はそれを巻き戻さない。帰結として **Terraform 側で env / secrets /
  サイズを変えても service は旧 revision のまま**動く (新 revision は作られるが
  反映されない)。infra 起因の変更は「apply 後に rollout を再実行」が正規手順。
  rollout は family の最新 ACTIVE revision を土台に image tag だけ差し替えるので、
  再実行すると Terraform 由来の変更もそこで取り込まれる。
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

## DB 踏み台 (enable_db_bastion)

RDSの初期構築・管理者保守用の一時経路で、通常migrationには使用しない。**平常時は存在せず、素の apply が
撤去を兼ねる**。SSM Session Manager の port forwarding で、踏み台は public IP も
SSH ポートも ingress 規則も持たない。踏み台という言葉が普通に指す「開いている
入口」は、ここには無い。

具体的なコマンド列は private runbook 側に置く。ここに書くのは境界だけ。

- **admin 専用なのは設計であって権限不足ではない。** 踏み台の role は Session
  Manager を使うため permissions boundary を付けられず (task 系の boundary が
  `ssmmessages:*` を Deny)、boundary 無しの role 作成は `terraform-apply` 側の
  `DenyRoleCreationWithoutBoundary` が拒否する。名前も boundary の対応表に無いので
  `DenyRoleCreationOutsideKnownRoles` でも拒否される。`ssm:StartSession` も
  `terraform-apply` は持たない。**CI 用の経路でこれが通らないのは fail-closed が
  効いた結果**で、穴を開けて通すものではない (bastion.tf の注記と対)。
- **踏み台の plan / apply / SSM / 撤去は最初から最後まで admin profile だけを使う。**
  各操作の前に `infra/aws/scripts/verify-aws-profile.sh vector-admin` で実 caller を
  検証する。通常migrationは専用workflowの承認後jobで実行し、踏み台やローカルprofile
  へ切り替える代替経路は用意しない。
- **トンネル越しに `verify-full` を保つ方法が client で違う。** libpq (psql) は
  `host` と `hostaddr` を分離指定できるが、**asyncpg / pg にこの分離は無い**。
  後者は名前解決の側で解く。証明書の検証を落として解決しない — `db_ssl.py` は
  「検証なし TLS というモードを持たない」と宣言しており、手順書側に抜け道を
  作るとその宣言が意味を失う。
- **踏み台を使う作業の最中に apply するなら必ず var を付ける。** 素の apply は
  設計どおり土管ごと撤去する。
- SSM のデータチャネルは数 MB/s。この DB の規模なら pg_restore に実害はない。
- `start-session` が TargetNotConnected のときの第一容疑者は endpoints SG
  (bastion からの 443 ingress は toggle 内の conditional resource)。

## DB の構築と migration が持つ制約

独立承認するmigration・アプリ反映の契約と本番有効化条件は
[Migrationとアプリ反映の導入・運用](MIGRATION_WORKFLOW.md) を参照する。

空のRDSの初期構築は通常migrationとは別作業であり、今回の切り替えでは実施しない。
private runbookには初期構築の前提を残し、通常migrationのローカル実行手順は置かない。

- **schema の作成主体が 2 つに割れている。** alembic の chain は `auth."user"`
  への FK を持つが、auth のテーブルは Better Auth CLI の管轄で alembic は作らない。
  **CLI を alembic より先に**流す必要がある (逆だと watchlist_entries の FK 作成で
  落ちる)。統合テストは `create_all` で schema を焼くため、**この順序依存は
  migration 経路でしか現れない** — テストが緑でも初期構築は落ちうる。
- **DB role は password を持たない。** `vector_app` / `vector_auth` /
  `vector_collect` とmigration ownerの`vector`はIAM認証 (`GRANT rds_iam`) を使い、
  `db-provision.sql`にもCIにも秘密を置かない。password認証はbreak-glassの
  `vector_master`だけに残す。
- **migrationとアプリ反映はそれぞれ手動起動・独立承認。** migrationはexpand / contract /
  verifyを明示し、mixedはCIとrunnerで拒否する。アプリ反映は最新mainのみを対象に、
  最新ledgerのrevisionとmigration treeが一致した場合だけ進む。自動dispatchやfreezeは無い。
- **初回構築は destructive gate を明示的に通す。** b1 の legacy テーブル削除が
  要求する。空 DB では失うものが無いので通してよい、という判断がその都度要る。

## egress proxy の残余

- **オープンプロキシ化はアドレスの不在が防いでいる。** proxy は private subnet に
  public IP 無しで置く。仮に 3128 の inbound SG を CIDR で緩めても、インターネット
  からこの task を宛先に指定する手段が無い。SG (app SG 参照のみ) と squid.conf の
  `src` ACL + 末尾の `deny all` は、その内側に残る自前設定 2 枚。
- **外向きの送信元 IP は EIP で固定される。** `terraform output egress_public_ip`。
  proxy を再デプロイしても変わらないので、外部ベンダー側の allowlist に登録できる。
- **非公開レンジの正本は app 側の 1 ファイル** (`backend/app/http/non_public_ranges.json`)。Terraform は `jsondecode(file(...))` で読んで
  squid.conf を生成し、app は実行時の判定に使う。**ポリシーの持ち主はアプリで、
  Squid は写し**。アプリはPythonのIP判定も加え、`TestNonPublicRangeParity`は
  同じIPについて **proxyの非公開レンジ拒否 ⊆ appの拒否** を確認する。
  DNS解決結果の一致や、ドメイン・ポートを含む全拒否条件の一致は保証しない。
  責任分担と通常のプロキシ経路は[HTTPの宛先方針](../../backend/app/http/README.md)を参照する。
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
- **AgentCore Gateway の web-search target** — `scripts/create-websearch-target.sh`。
  provider 6.62 の `aws_bedrockagentcore_gateway_target` は connector を持たず
  (target 種別は api_gateway / lambda / mcp_server / open_api_schema / smithy_model)、
  awscc にも `gateway_target` 資源が無いため Cloud Control 経由でも書けない。
  API 側にだけ connector があるので CLI で作る。**apply の後に 1 回実行する。**
  provider が対応したら `terraform import` で畳む
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

## Outbox relay Lambda

工程別SQS・relay専用Lambdaの初回構築、digest更新、接続確認は [OUTBOX_RELAY.md](OUTBOX_RELAY.md) を参照する。現在のhandlerはOutboxRelay.run_onceでembedding向けイベントを1回最大10件送信する。スライス4.2では既存SchedulerをENABLEDへ更新し、送信開始後に実処理を確認する。適用・監視・緊急停止は同手順を参照する。

## EmbeddingConsumer Lambda（スライス3.4）

共通backendイメージをarm64のLambdaとして起動する。Consumerの版は`embedding_consumer_image_digest`で独立指定し、ECSのimage_tagやrelayのdigestとは連動させない。メモリ1024MB、timeout120秒、予約同時実行10、1回1件、最大同時実行10、ReportBatchItemFailuresで固定する。スライス3.4ではSQSトリガーを`enabled=false`で配置した。スライス4.1以降の目標状態は`enabled=true`であり、既存マッピングの更新・今後の新規作成ともに受信を有効にする。relayのSchedulerはスライス4.1では`DISABLED`を維持した。スライス4.2で`ENABLED`へ更新する。

関数は専用サブネット・SG・実行ロール・ロググループを使用する。環境変数はproduction、IAM認証のvector_app用DB URL、専用Gemini SSMパス、EGRESS_PROXY_URLを渡す。AWS_REGIONはLambdaが提供する。APIキーの値はTerraform・イメージ・ログへ置かず、Consumer呼び出し時にSSMから取得する。SSMの準備は有効化前に行い、値の登録と実通信検証は別作業とする。

### 初回構築・更新・切り戻し

1. 管理者経路でbootstrapを先に適用し、`ci-apply-embedding-consumer`ポリシーとapplyロールへの接続、plan/apply向けの限定Lambda復号ポリシーを反映する。[Lambda設定の読戻し確認](bootstrap/README.md#lambda管理設定の読戻し権限)を済ませてから本体planへ進む。今回、実行ロールのboundaryとPassRoleの制約は変更しない。
2. 既存のAWS app images workflowで対象mainの共通backendイメージを作成・公開し、backend ECRのsha256 digestを取得する。Consumer専用イメージは作らない。既存ECSのrollout承認は別工程であり、Consumerの更新はTerraform経路で行う。
3. AWS terraform applyをmainで手動実行し、`embedding_consumer_image_digest`へ対象digestを入力する。backendリポジトリ内の存在確認が成功した場合だけplanへ進む。production承認後、既存どおり同じjobでplanを再実行してapplyする。受信状態は現在のTerraform定義に従うため、新規作成も有効になる。SSM登録と受信開始の確認を済ませてから承認する。
4. outputsの`embedding_consumer_function_name`・`embedding_consumer_function_arn`・`embedding_consumer_image_digest`・`embedding_consumer_event_source_mapping_uuid`を確認する。AWSのGetFunctionConfigurationとGetEventSourceMappingで実行設定とState=Enabled、relayのSchedulerが現在のTerraform定義（スライス4.2以降はENABLED）と一致することを確認する。この確認では関数をinvokeしない。
5. 通常のinfra適用では入力を空欄にして現在の版を維持する。更新・切り戻しは新しい版・過去の版のdigestを明示する。既にECRから削除された版は指定できないため、切り戻し先の保持状況も確認する。
6. 配置済みConsumerの受信有効化・停止・再開は以下の手順に従う。relayの定期送信開始は[スライス4.2](OUTBOX_RELAY.md#定期送信の開始と監視スライス42)に従い、実通信・ログ配送・再配信・DLQ移動・Taskiq併用の実証は開始後に記録する。

初回にdigestを指定しない場合、基盤は維持するが関数とトリガーは作成せず、上記outputsはnullとなる。SSOの期限切れやstate解決失敗を初回扱いにしない。

### Consumerの受信有効化・監視・停止・再開（スライス4.1）

以下の開始手順はConsumerだけを先に有効化したスライス4.1の記録であり、relayのDISABLED確認は当時の条件である。現在のrelay開始は[スライス4.2](OUTBOX_RELAY.md#定期送信の開始と監視スライス42)を使う。ユーザーの選択でOutboxの事前件数確認は省略し、開始後に実処理を確認する。以下のConsumer停止・再開手順は引き続き使用する。bootstrap再適用、イメージ再ビルド・公開、digestの変更は不要。

**適用前の確認と受信開始**

管理者のAWS CLIでアカウント・東京リージョンを確認する。既存のSSOログイン後、以下は参照だけを行う。SSMはメタデータだけを取得し、秘密値やLambdaの環境変数値は表示しない。

```bash
bash <<'BASH'
set -euo pipefail
export AWS_PROFILE=vector-admin AWS_REGION=ap-northeast-1 AWS_PAGER=""
aws sts get-caller-identity --query Account --output text
aws ssm describe-parameters \
  --parameter-filters 'Key=Name,Option=Equals,Values=/vector/embedding-consumer/gemini-api-key' \
  --query 'Parameters[].{Name:Name,Type:Type,Tier:Tier,KeyId:KeyId}' --output json
aws lambda get-function-configuration --function-name vector-embedding-consumer \
  --query '{State:State,Update:LastUpdateStatus,Memory:MemorySize,Timeout:Timeout,EnvironmentError:Environment.Error.ErrorCode,ImageConfigError:ImageConfigResponse.Error.ErrorCode}' --output json
for queue_name in vector-article-embedding vector-article-embedding-dlq; do
  queue_url=$(aws sqs get-queue-url --queue-name "$queue_name" --query QueueUrl --output text)
  printf '%s\n' "$queue_name"
  aws sqs get-queue-attributes --queue-url "$queue_url" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible ApproximateNumberOfMessagesDelayed \
    --query Attributes --output json
done
aws scheduler get-schedule --group-name vector-outbox-relay --name vector-outbox-relay \
  --query '{State:State,Schedule:ScheduleExpression}' --output json
BASH
```

SSMが指定名のSecureString・Standard・alias/aws/ssm、LambdaがActive・Successful・1024MB・120秒、設定取得エラーなしであることを確認する。キューにメッセージがあれば有効化により処理が始まるため件数を確認し、relayがDISABLEDであることも確認する。

PR planでは既存のConsumer・relayのdigest保持処理を使い、変更が既存マッピングの`enabled: false → true`だけであることを確認する。関数・キューの再作成や削除、relay変更、環境変数・起動設定の不要差分があれば、そのまま適用しない。マージで起動する最新のAWS terraform applyをproduction承認し、digest入力を追加せず現在の版を保持する。同じ変更の手動runを重複起動しない。

適用後、以下で対象マッピングのEnabled・BatchSize=1・収集待ち0秒・最大同時実行10・ReportBatchItemFailures、予約同時実行10、relayのDISABLEDを確認する。関数と元キューのARNも対象が正しいことを確認する。

```bash
bash <<'BASH'
set -euo pipefail
export AWS_PROFILE=vector-admin AWS_REGION=ap-northeast-1 AWS_PAGER=""
aws lambda list-event-source-mappings --function-name vector-embedding-consumer \
  --query 'EventSourceMappings[].{UUID:UUID,Function:FunctionArn,Queue:EventSourceArn,State:State,BatchSize:BatchSize,Window:MaximumBatchingWindowInSeconds,MaximumConcurrency:ScalingConfig.MaximumConcurrency,Responses:FunctionResponseTypes}' --output json
aws lambda get-function-concurrency --function-name vector-embedding-consumer --output json
aws scheduler get-schedule --group-name vector-outbox-relay --name vector-outbox-relay \
  --query '{State:State}' --output json
BASH
```

適用後の読み取り専用planでも、以下のdigest保持手順と既存ECS image_tag保持を使って不要な差分がないことを確認する。ローカルplan用SSOが利用できない場合は未実施として残し、実CIロールでの確認機会に実施する。確認のためだけに本体applyを再起動しない。

**監視と判断**

ロググループ`/aws/lambda/vector-embedding-consumer`で`embedding_initialization_failed`、`embedding_message_input_invalid`、`embedding_message_failed`、`embedding_message_completed`と既存の処理結果計測を確認する。LambdaのErrors・Duration・Throttles・ConcurrentExecutions、元キューの可視件数・処理中件数・最古メッセージ経過時間、DLQ件数と既存の滞留アラーム、RDSのCPU・接続数・空きメモリを確認する。新しいメトリクスやアラームは追加しない。

部分バッチ応答の失敗はLambdaのErrorsだけでは判断しない。relay停止中かつキューが空なら実行がないことを異常とせず、受信有効化だけで実処理を検証済みとしない。既存Taskiqは並行稼働を維持する。

SSM・DB・プロキシ・認証の共通障害が複数メッセージで繰り返される場合や、継続する設定不備が判明した場合は手動停止する。個別記事の失敗は既存の再配信・DLQへ任せ、自動停止やDLQからの自動再投入は追加しない。

**緊急停止（このブロックは実際に受信を停止する）**

以下は`vector-admin`で東京の対象関数と元キューに一致するマッピングを特定し、1件だけであることとUUIDを検証して停止する。失敗した場合は対象を推測して続行しない。CLIの停止はポーリングと新規呼び出しを停止する操作であり、実行中の処理を強制終了したり、メッセージを削除したりしない。[AWS CLI仕様](https://docs.aws.amazon.com/cli/latest/reference/lambda/update-event-source-mapping.html)

```bash
bash <<'BASH'
set -euo pipefail
export AWS_PROFILE=vector-admin AWS_REGION=ap-northeast-1 AWS_PAGER=""
account_id=$(aws sts get-caller-identity --query Account --output text)
if ! [[ "$account_id" =~ ^[0-9]{12}$ ]]; then
  printf '%s\n' 'AWSアカウントを確認できないため停止します。' >&2; exit 1
fi
function_arn="arn:aws:lambda:${AWS_REGION}:${account_id}:function:vector-embedding-consumer"
queue_url=$(aws sqs get-queue-url --queue-name vector-article-embedding --query QueueUrl --output text)
queue_arn=$(aws sqs get-queue-attributes --queue-url "$queue_url" --attribute-names QueueArn --query Attributes.QueueArn --output text)
if ! [[ "$queue_arn" == "arn:aws:sqs:${AWS_REGION}:${account_id}:vector-article-embedding" ]]; then
  printf '%s\n' '元キューのARNが一致しないため停止します。' >&2; exit 1
fi
mappings=$(aws lambda list-event-source-mappings --function-name "$function_arn" --event-source-arn "$queue_arn" --output json)
mapping_uuid=$(printf '%s' "$mappings" | jq -er --arg function "$function_arn" --arg queue "$queue_arn" '
  .EventSourceMappings |
  if length == 1 and .[0].FunctionArn == $function and .[0].EventSourceArn == $queue
  then .[0].UUID else error("対象関数・元キューに一致するマッピングを1件に特定できない") end')
if ! [[ "$mapping_uuid" =~ ^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$ ]]; then
  printf '%s\n' 'UUIDが不正なため停止します。' >&2; exit 1
fi
aws lambda update-event-source-mapping --uuid "$mapping_uuid" --no-enabled \
  --query '{UUID:UUID,State:State}' --output json
for attempt in {1..12}; do
  state=$(aws lambda get-event-source-mapping --uuid "$mapping_uuid" --query State --output text)
  case "$state" in
    Disabled) printf '受信停止を確認: %s\n' "$mapping_uuid"; exit 0 ;;
    Disabling|Updating|Enabled) sleep 5 ;;
    *) printf '停止状態を確認できない: %s\n' "$state" >&2; exit 1 ;;
  esac
done
printf '%s\n' '停止完了を確認できないため、AWS上の状態を確認してください。' >&2
exit 1
BASH
```

停止中は受信を再有効化する本体applyを承認しない。AWSだけを無効にすると、Terraformのenabled=trueが次回applyで復元されるため、コードと対応テストもenabled=falseにしてPR・マージ・承認付きapplyで停止状態を反映する。stateの手編集やignore_changesは追加しない。

原因を解消してからenabled=trueと対応テストを戻し、PR plan・マージ・production承認付きapplyで再開する。再開時もdigestを保持し、Enabledの確認と監視を行う。DLQ再投入の具体的操作は別タスクとする。

### ローカルplanでのdigest保持

Terraform変数のdefault=nullだけでは現在の版は保持できない。既存の使い方の`terraform plan`を実行する前に、relayとConsumerの両方について次を実行する。通常のplan/apply workflowはこの処理を実装済み。ローカルの本番applyは実行せず、既存の承認付きCI経路を使用する。

```bash
# infra/awsで、認証とbackendの初期化後に実行する。
set -euo pipefail
relay_vars_tmp=$(mktemp ./outbox-relay-vars.XXXXXX)
consumer_vars_tmp=$(mktemp ./embedding-consumer-vars.XXXXXX)
assessment_vars_tmp=$(mktemp ./assessment-vars.XXXXXX)
curation_vars_tmp=$(mktemp ./curation-vars.XXXXXX)
trap 'rm -f "$relay_vars_tmp" "$consumer_vars_tmp" "$assessment_vars_tmp" "$curation_vars_tmp"' EXIT
terraform state pull | python3 scripts/resolve-outbox-relay-image.py > "$relay_vars_tmp"
terraform state pull | python3 scripts/resolve-embedding-consumer-image.py > "$consumer_vars_tmp"
terraform state pull | python3 scripts/resolve-assessment-images.py > "$assessment_vars_tmp"
terraform state pull | python3 scripts/resolve-curation-images.py > "$curation_vars_tmp"
mv "$relay_vars_tmp" outbox-relay.auto.tfvars.json
mv "$consumer_vars_tmp" embedding-consumer.auto.tfvars.json
mv "$assessment_vars_tmp" assessment.auto.tfvars.json
mv "$curation_vars_tmp" curation.auto.tfvars.json
```

明示した版をplanする場合はConsumerのスクリプトへ`--digest "$CONSUMER_DIGEST"`を渡し、backend ECR内の存在も別途確認する。生成ファイルやstateをコミット・公開しない。stateの現行イメージが不正、現行インスタンスが複数、state取得失敗の場合は停止し、ファイルを手作業でnullへ変更して続行しない。

### ローカルのイメージ起動検証

リポジトリルートで実行する。AWSや外部AIに到達しないネットワークで、SSM・DB資源をモックし、実Geminiクライアント・Consumer・handlerをRICから呼ぶ。2回の空Recordsで応答と呼び出しごとの資源終了を検証する。業務処理は既存単体・実DB統合テストで検証する。

```bash
docker build --platform linux/arm64 -t vector-embedding-slice34:local backend
docker run --rm --platform linux/arm64 --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m --network none \
  -v "$PWD/infra/aws/scripts/verify-embedding-runtime.py:/verify-runtime.py:ro" \
  --entrypoint /app/.venv/bin/python \
  vector-embedding-slice34:local /verify-runtime.py
```

LambdaのCI管理権限はConsumer関数ARNとFunctionArn条件で限定する。マッピングのConsumerタグは作成時に必須とし、タグ操作も既存のConsumerタグ一致を要求するため、他のマッピングへタグを後付けして管理範囲を拡張できない。Consumerタグの変更・削除は許可しない。タグ付き作成とResourceTag条件の考え方は[AWSのABAC例](https://docs.aws.amazon.com/lambda/latest/dg/attribute-based-access-control-example.html)に合わせ、AWS上での適用確認は後続に残す。

## Curationの配置（スライス6前半）

今回追加するのはTerraform・bootstrap・既存GitHub Actionsのplan／apply設定であり、AWSへの適用・キー登録・digest指定は行っていない。独立したデプロイ経路は追加しない。

- Consumerは`${name_prefix}-curation-consumer`、arm64・1,024MB・120秒・予約同時実行10。専用subnetのCIDRは`cidrsubnet(var.vpc_cidr, 8, 32)`で、RDS・Gemini proxy・SSMへの通信だけを許可する。
- relayは`${name_prefix}-curation-outbox-relay`、arm64・512MB・120秒・予約同時実行1。既存のOutbox relay用通信経路からCurationキューへ送り、専用Schedulerが1分間隔で起動する。
- 既存`outbox["curation"]`の保持期間は14日から4日、可視性は30秒から720秒へ変更する。5回の受信後に専用DLQ（保持14日）へ移し、可視メッセージ1件以上で既存SNSへ通知する。保持期間短縮により既に4日を超えたメッセージが期限切れになるため、適用前に滞留を確認する。
- digest未指定なら対応Lambda・mapping／scheduleは作成しない。指定時は作成と有効化を同時に行う。受信はバッチ1・待機0秒・最大同時実行10・`ReportBatchItemFailures`。

### 後続タスクでの適用順序

1. **bootstrap先行**: 管理者の既存bootstrap経路で専用boundaryとCI権限を反映する。本体CIにはbootstrap変更権限を追加しない。policy移動を含むため`-target`で部分適用しない。
2. **基盤・キー準備**: 両digestを初回nullのまま、既存のproduction承認付きTerraform applyでキュー・DLQ・subnet・SG・ロール・ログ・proxy設定を配置する。既存proxy serviceはTerraformのtask definition更新に追従するため、本体applyでCurationの許可CIDRを反映し、通信を確認する。`/${name_prefix}/curation-consumer/gemini-api-key`をSecureString・既存`alias/aws/ssm`で管理者がTerraform外から登録する。実値はコマンド引数・tfvars・state・CIログへ渡さない。
3. **Consumer有効化**: backend ECRに存在するarm64イメージを選び、既存`AWS terraform apply`の`curation_consumer_image_digest`だけを指定して承認付き実行を行う。relay入力は省略する。ConsumerのActive／Successful、mappingのEnabled・対象キュー・受信設定、SSM・RDS IAM・proxy接続を確認する。キューに既存メッセージがあればこの時点で処理が始まる。
4. **通常経路切替時にrelay有効化**: 上流Taskiq直接投入の終了と合わせる後続タスクで`curation_outbox_relay_image_digest`を指定する。Consumer入力の省略はstateの現行値を保持する。実配送・DB保存・部分バッチ応答・DLQ移動・SNS通知を確認する。旧救済は別タスクとして扱う。

通常のplan／applyでは`resolve-curation-images.py`が現行digestを保持する。片側更新・切り戻しは対応入力だけに明示digestを渡す。state不正・digest不正・明示イメージのECR不在なら停止し、nullやタグへ置き換えて続行しない。digest省略を停止操作として使わない。ローカルの読み取り専用planでも上記の全工程のdigest保持処理を行い、生成JSON・stateはコミットしない。

確認用outputsは`curation_consumer_*`（関数・digest・subnet・SG・ロール・ログ・SSM参照・mapping UUID）、`curation_outbox_relay_*`（関数・digest・ロール・ログ・Scheduler）、`curation_dlq_*`、既存`outbox_queue_urls`／`outbox_queue_arns`を使用する。実AWSの権限成立・配送・通知はmock planの成功だけでは検証済みとしない。

### 今回のローカル検証範囲

本体・bootstrapのmock planとdigestスクリプトの実CLIテストを主な保証とする。実state・tfvarsをコピーしない一時ディレクトリで`terraform fmt -check`、`init -backend=false -input=false -lockfile=readonly`、`validate`、`test`を実行する。Pythonのlint・formatと`python3 -m unittest discover -s infra/aws/scripts -p 'test_*.py'`、変更したworkflowのactionlintも実行する。既存のbackend業務コード・業務テストは変更しない。`backend/tests/scripts/test_assessment_infrastructure.py`の既存CI shellテストへCurationを追加し、state取得失敗時の設定非配置・一時ファイル解放と、明示イメージがECRにない場合の停止を共有テストで確認する。

actionlint 1.7.12は既存の`concurrency.queue: max`を未対応キーとして扱う。この1種類だけを`-ignore 'unexpected key "queue" for "concurrency" section'`で除外し、残りを検証する。既存値は[GitHub公式のqueue仕様](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)と照合し、productionの順次実行設定を変更しない。
