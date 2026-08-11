# CloudWatch Symptom-Based Alerting

作成: 2026-08-11
Status: Draft (レビュー中 — 途絶 3 層構成・AI 利用枠枯渇まで合意済み)

---

## Work Definition

### Problem

本番の観測・アラート層は Logfire(SaaS UI での手動設定、IaC 外)に依存しており、CloudWatch にはログが届いているだけでアラートが 1 本も存在しない。2026-06-08 には収集が 48.5 時間停止しても検知できなかった。

「実際に問題が起きている事象」だけを対象にした Terraform 管理の CloudWatch アラート基盤を設計する。

### Evidence

- ECS 全 7 サービスの stdout は awslogs で `/ecs/vector/*` に集約済み(`infra/aws/ecs.tf`、retention 30 日)。metric filter / alarm / dashboard / SNS は Terraform に存在しない。
- Container Insights は意図的に無効(`infra/aws/ecs.tf` コメント: Logfire で賄うため二重に払わない)。
- ALB target group は frontend のみ。ブラウザからの API 呼び出しも Next.js proxy 経由で frontend を通るため、ユーザー向けリクエストは実質すべて ALB メトリクス(`HTTPCode_*_5XX_Count` / `UnHealthyHostCount` / `TargetResponseTime`)に乗る。無料。
- アプリメトリクス 43 種(`vector.*`)は Logfire にのみ送信。CloudWatch には届いていない。
- cron 時刻表の SSoT は `backend/app/queue/schedule.py`: dispatch_high 15 分間隔 / medium 1 時間 / low 6 時間、completion 系は毎分、backfill 系は 30 分間隔。
- `observe_pipeline_queue_health`(`backend/app/queue/tasks/queue_health.py`)が毎分、acquisition / completion / curation / assessment の 4 stream について `oldest_outstanding_enqueue_age`(最古の未処理 entry の経過秒数)等を stage 属性付き gauge で Logfire に記録している。観測失敗時は `observation_up=0`。**embedding と dispatch の stream は観測対象外**(`PIPELINE_QUEUE_TARGETS` 固定 4 stage)。
- queue_health は analysis サービス内の maintenance worker(`supervisord/analysis.conf`)で動く。maintenance worker は backfill 救済・retention purge も担う。
- AI provider エラーは翻訳層で分類済み(`app/analysis/gemini_error_translator.py` / `deepseek_error_translator.py`): 一時的な `AIProviderRateLimitedError` と、利用枠の枯渇である `AIProviderUsageLimitExhaustedError`(Gemini 429 の quota/daily)・`AIProviderInsufficientBalanceError`(DeepSeek 残高切れ)を区別している。
- 自前の rate limit gate(`app/analysis/rate_limit/`)は provider quota を先回りして AI call を skip し、`vector.analysis.rate_limit_gate_skipped{stage,model}` を Logfire に記録している。
- agent(Q&A)の runtime(`app/agent/runtime/gemini.py` / `deepseek.py`)も同じ翻訳層(`translate_gemini_error` / `translate_deepseek_error`)を再利用しており、枯渇系エラーの語彙は pipeline と共通。
- 2026-06-08 incident: worker-fetch 停止(SPOF)で dispatch task が実行されず収集が途絶。dispatch が死ぬと下流 stream には何も積まれないため、下流の滞留観測では原理的に検知できない。
- scheduler は singleton 構成。HTTP を持たず、死活は ALB では観測できない。

### Invariants(アラート設計原則)

- アラートは「ユーザーか業務に実害が出ている・確実に出る事象」のみに張る。原因側指標(CPU/メモリ使用率)はアラートにしない。
- 全アラートは受信時に取るべきアクションを 1 行以上定義する。アクションの無い通知は作らない。
- 一時的な rate limit(バースト超過・自前 gate の pacing)はアラートにしない。実害(処理の滞留)が出れば A2 が拾う。利用枠の**枯渇**(残高切れ・日次 quota 切れ)は行動が必要な事象なのでアラートにする(A6)。
- アラート定義・通知経路は Terraform 管理とする。コンソール手動作成はしない(Slack workspace の初回 OAuth 承認のみ例外)。
- CloudWatch カスタムメトリクスは「alarm または dashboard という consumer がいるもの」だけ emit する。Logfire メトリクスの全量移送はしない。
- metric の dimension に PII・URL・記事 ID・source 名を載せない(pipeline_events と同じ規律)。
- Slack channel ID / workspace ID などの実値は commit しない(Public Repository Hygiene)。Terraform variable とし、値は非コミットの tfvars / GitHub Environment 側で渡す。
- pipeline_events(DB 監査)は変更しない。

### Non-goals

- Logfire の trace / span の移行(当面残す。撤去判断は別タスク)。
- 応答時間(latency)アラート。SLO 未定義のため作らない。SLO を定義したくなったら既存無料メトリクス `TargetResponseTime` に alarm 1 本で後付けできる。
- CPU / メモリ使用率の閾値アラート。OOM は発生事象(A5)で拾う。
- Container Insights の有効化。
- ダッシュボード設計(別 spec。本 spec は「鳴るもの」だけを扱う)。
- assessment 以外の工程の失敗率アラート(次ステップで工程ごとに分母の扱いを詰めてから拡張する)。
- 一時的 rate limit・自前 gate skip のアラート化(上記 Invariant の通り)。
- backfill daily budget 枯渇のアラート化(救済経路の意図的な上限。異常な backlog 成長は A2 が拾う)。
- agent のユーザー向け日次クォータ(回数制限)枯渇のアラート化(意図的なプロダクト上限で、利用者に 429 として直接見える。AI provider の枠枯渇とは別物)。
- dev 環境のアラート。

### Done

- アラート 8 定義(条件・評価期間・missing data の扱い)が確定している。
- 新規に emit する EMF メトリクスの契約(名前・dimension・emit point)が確定している。
- 通知経路(SNS → Slack)と、公開 repo に載せない値の受け渡し方法が確定している。
- 実装順序(1 アラートずつ)が確定している。

---

## 1. アラートカタログ

| ID | 症状 | 検知シグナル | 種別 |
|----|------|-------------|------|
| A1 | 収集の供給が止まっている(全体途絶) | EMF `dispatch_run` の不在 | metric alarm |
| A2 | 特定工程で仕事が消化されていない(工程名指し) | EMF `oldest_outstanding_enqueue_age{stage}` | metric alarm × 5 |
| A3 | queue 観測自体が死んでいる(Valkey 障害含む) | EMF `observation_up` | metric alarm (math MIN) |
| A4 | assessment の失敗率が急増 | EMF `assessment_outcome` の failed 率 | metric alarm (math) |
| A5 | ECS タスクの異常停止(crash / OOM / 起動不能) | EventBridge ECS Task State Change | event 通知 |
| A6 | AI 利用枠の枯渇(残高切れ・日次 quota 切れ) | EMF `ai_provider_exhausted` | metric alarm |
| A7 | ユーザーにエラーが見えている | ALB 5XX | metric alarm |
| A8 | frontend が到達不能 | ALB UnHealthyHostCount | metric alarm |

「止まっている」の検知は A1 / A2 / A3 の 3 層で役割分担する。検知原理が異なるため 1 本にまとめない。

### A1: 供給ハートビート(全体途絶)

- 条件: `Vector/Pipeline` `dispatch_run{cadence=high}` の Sum、period 1h、2 evaluation periods 連続で 0。`TreatMissingData = breaching`(emit が無い = 途絶とみなす)。
- 根拠: dispatch_high は 15 分間隔なので 1h に 4 回期待。2h 無音は確実に異常。emit は dispatch task の**正常完了時**のみとし、scheduler 死・broker 死・dispatch worker 死・DB 死のいずれでも鳴る。
- 工程を名指ししないのは役割分担: 供給源(cron 駆動)が止まると下流には滞留が発生しないため、下流観測(A2)では検知できない。2026-06-08 の障害はこのケース。
- アクション: scheduler / fetch サービスのログ確認 → 停止プロセスの再起動。

### A2: 工程別の滞留(工程を名指し)

- 症状: 「その工程に仕事が積まれたまま、閾値時間を超えて消化されていない」。
- 条件: `oldest_outstanding_enqueue_age{stage}` の Max、period 5min。閾値の初期値は acquisition / completion = **30 分**、curation / assessment / embedding = **60 分**(AI 工程は rate limit pacing による正当な滞留がありうるため)。実測で調整する前提。`TreatMissingData = notBreaching`(観測死は A3 が担当。仕事ゼロのときは age=0 が emit されるので、生きていれば missing にならない)。
- alarm は stage ごとに 1 本(計 5 本)。alarm 名と説明文に工程名を焼き、Slack 通知が「assessment 工程が停止しています」とそのまま読めるようにする。
- 量に依存しない: 新着ゼロの時間帯は age=0 で鳴らず、仕事があるのに consumer が死んでいれば age が線形に伸びて確実に鳴る。工程別の活動量 missing data 検知が持つ「閑散時間帯の誤発火」を原理的に回避する。
- データ源: 既存の queue_health 観測を EMF に二重 sink する。**embedding stream を `PIPELINE_QUEUE_TARGETS` に追加する**(alarm という consumer が新たにできたため。dispatch stream は追加しない — A1 の担当)。
- アクション: 該当工程の worker ログ確認 → 再起動。rate limit 起因なら pacing 設定と backlog を確認。

### A3: 観測の死活(メタ監視)

- 症状: A2 の前提である queue_health 観測が動いていない。Valkey 障害(snapshot 取得失敗 → `observation_up=0`)と、maintenance worker / scheduler 死(emit 消失 → missing)の両方を拾う。
- 条件: `observation_up{stage}` 全系列の metric math `MIN(...)` < 1、period 5min、1 evaluation period。`TreatMissingData = breaching`。
- Valkey 全面障害は本 alarm(約 5 分)+ A1 で検知される。単一 stream の異常(group 消失等)は該当 stage のみ 0 になる。
- maintenance worker は backfill 救済・retention も担うため、この死活は観測だけでなく救済機能の停止シグナルでもある。
- アクション: `observation_up=0` なら Valkey / stream の状態確認。missing なら maintenance worker / scheduler の生死確認。

### A4: assessment 失敗率の急増

- 条件: metric math `IF(total >= 10, failed / total, 0) >= 0.5`、period 1h、2 evaluation periods 連続。`total = succeeded + rejected + failed`(分母の扱いは `logfire-assessment-outcome-metrics.md` の既存不変条件を踏襲し、冪等 skip / infra_error は分母から除外)。`TreatMissingData = notBreaching`(新着ゼロの時間帯は正常)。
- 根拠: 既知パターン = DeepSeek 不正 JSON で 54% 失敗が数日継続。最小標本 10 で低トラフィック時の誤発火を防ぐ。
- アクション: analysis worker ログと admin pipeline_health で error_class を確認。

### A5: ECS タスク異常停止(crash / OOM / 起動不能)

- 条件: EventBridge rule — source `aws.ecs`, detail-type `ECS Task State Change`, `detail.lastStatus = STOPPED`, `detail.stopCode` ∈ {`EssentialContainerExited`, `TaskFailedToStart`}(allowlist)。stopCode の値域は ECS API Reference で 6 値確定(他は `UserInitiated` / `ServiceSchedulerInitiated` / `SpotInterruption` / `TerminationNotice`)。
- デプロイ由来の旧タスク停止は `ServiceSchedulerInitiated` になるため allowlist 外 = ノイズにならない。graceful stop の exit 143 は container 側の値で、task の stopCode 判定には影響しない。
- 既知の穴と手当: ELB ヘルスチェック失敗起因の kill も `ServiceSchedulerInitiated` になり本 rule では拾えないが、その症状は A8(UnHealthyHostCount)が正面から検知する(役割分担)。
- 通知整形: EventBridge 発の生イベントは Q Developer chat に配送されない場合がある(「Event received is not supported」)ため、input transformer で custom notification schema(`version: 1.0` / `source: custom` / `content.description` 必須)へ変換してから SNS target に流す。OOM の識別は、この整形で `detail.containers[].exitCode`(OOM = 137)を本文に埋め込むことで行う。
- rule は stopCode 別に 2 本(crashed / failed-to-start)に分ける。`TaskFailedToStart` のイベントには `containers[].exitCode` が無いことがあり、input_paths の欠損は配送失敗になり得るため、exitCode を参照する template を `EssentialContainerExited` 側に閉じる。
- OOM 検知は旧 memory-monitoring.md フェーズ 2(Fly の OOM 確定検知)の AWS 版置き換え。
- スパム許容: crash loop 時は停止ごとに 1 通届く。desired_count = 1 規模では初期割り切りとして許容。
- アクション: 該当サービスのログ確認。exit 137 ならメモリサイジング見直し。

### A6: AI 利用枠の枯渇

- 症状: AI provider の利用枠が尽き、以後の AI 処理が枠回復まで全て失敗する状態。一時的な rate limit とは区別する(翻訳層が既に区別済み)。
  - `AIProviderInsufficientBalanceError` — DeepSeek 残高切れ。アクション: 残高チャージ。
  - `AIProviderUsageLimitExhaustedError` — Gemini の quota / daily 枠切れ。アクション: 枠リセット待ちか tier 引き上げの判断。
- Signal: EMF counter `ai_provider_exhausted{kind, provider}`、kind ∈ {insufficient_balance, usage_limit_exhausted}(≤ 4 系列)。emit point はエラー分類が確定する各 stage の failure handling 境界(分類ロジックは翻訳層 1 か所のまま、emit は決定境界の所有者が行う)。
- 条件: Sum >= 1、period 15min、1 evaluation period。`TreatMissingData = notBreaching`。発生が続く限り ALARM に留まり、枠回復後の成功で OK(復旧通知)が届く。
- スコープは analysis 3 工程(curation / assessment / embedding)と agent(Q&A)の provider 呼び出しの両方。agent runtime は同じ翻訳層を再利用しているため語彙は共通。emit point は analysis 側 = 各 stage の failure handling 境界、agent 側 = runtime の分類確定境界(`classified_error` 確定点)。
- stage / surface(pipeline・agent 別)の dimension は持たせない: 残高チャージ・枠回復というアクションは provider 単位で同一であり、どこが最初に踏んだかはアクションに影響しない。系列数は {kind, provider} の ≤ 4 のまま。
- 実測で 1 件発火がノイジーなら閾値を 3/15min へ調整。

### A7: ALB 5XX

- 条件: metric math `HTTPCode_Target_5XX_Count + HTTPCode_ELB_5XX_Count` の Sum >= 5、period 5min、1 evaluation period。`TreatMissingData = notBreaching`。
- 根拠: 低トラフィックのため率ではなく絶対数。ELB_5XX(ターゲット到達不能)も合算し、frontend 全滅も同じ alarm で拾う。
- アクション: frontend / api ログと直近 deploy を確認。

### A8: ALB UnHealthyHostCount

- 条件: Max >= 1、period 1min、5 evaluation periods 連続(5 分継続)。`TreatMissingData = notBreaching`。
- 根拠: desired 1 なので unhealthy 1 = frontend 全停止。瞬断(再起動 1 回)では鳴らさない。
- A5 / A7 との重複は意図的(event 経路と symptom 経路の冗長化)。

### 既知障害モードとのカバレッジ確認

| 障害モード(実績 / 想定) | 拾う定義 | Slack で分かること |
|---|---|---|
| scheduler / dispatch worker 死 | A1(+ A3 missing 側) | 供給が止まった |
| collection worker 死(2026-06-08 実績) | A2 acquisition / completion | 工程名指し |
| analysis worker(curation / assessment)死 | A2 該当 stage | 工程名指し |
| embedding worker 死 | A2 embedding | 工程名指し |
| maintenance worker 死 | A3(missing) | 観測と救済が止まった |
| Valkey(broker)全面障害 | A3(up=0、約 5 分)+ A1 | broker 障害と推定可能 |
| DeepSeek 残高切れ | A6(insufficient_balance) | チャージが必要 |
| Gemini 日次 quota 切れ | A6(usage_limit_exhausted) | 枠リセット待ち判断 |
| DeepSeek 不正 JSON 大量失敗(実績) | A4 | 失敗率と工程 |
| 一時的 rate limit / gate pacing | 鳴らさない(滞留すれば A2) | — |
| api 停止 | A7(SSR 経由 5XX), A5 | — |
| frontend 停止 | A8, A7, A5 | — |
| RDS 障害 | A1(dispatch 失敗), A7 | — |
| OOM kill | A5(exit 137) | サービスと exit code |

## 2. EMF メトリクス契約(新規 emit)

CloudWatch Embedded Metric Format で stdout に emit する。awslogs 経由で自動的にメトリクス化されるため、新しい egress 経路も SDK 送信も不要。

- Namespace: `Vector/Pipeline`
- `dispatch_run` — dimension `cadence` ∈ {high, medium, low}(3 系列)。dispatch task の**正常完了時**に 1。失敗時は emit しない。alarm consumer は high のみ、medium / low は将来の dashboard consumer 前提。
- `oldest_outstanding_enqueue_age` — dimension `stage` × 5(acquisition / completion / curation / assessment / embedding)。queue_health の毎分観測を Logfire gauge と EMF の二重 sink にする。仕事が無いときは 0 を emit(既存の `_age_or_zero` と同じ)。
- `observation_up` — dimension `stage` × 5。既存セマンティクス(成功 1 / 失敗 0)のまま二重 sink。
- `assessment_outcome` — dimension `result`(5 系列)。emit point・分類境界は既存 Logfire metric `vector.assessment.processing_outcome{result}` と同一。分類ロジックは 1 か所、sink が 2 つ。
- `ai_provider_exhausted` — dimension `kind` × `provider`(≤ 4 系列)。emit point は A6 の通り。
- 実装方式: EMF は公開安定仕様の JSON 形式なので、依存追加せず stdout へ 1 行 JSON を書く薄い helper を第一候補とする(`aws-embedded-metrics` 採用は依存追加になるため Ask First 対象)。書式は公式仕様で確認済み: root の `_aws.Timestamp`(epoch ミリ秒)+ `_aws.CloudWatchMetrics[]`(Namespace / Dimensions / Metrics)、metric・dimension の値は root 直下に置く。StorageResolution は既定の 60 秒でよい。
- 抽出経路: PutLogEvents 経由なら特別なヘッダー不要と公式に明記されており、awslogs ドライバは PutLogEvents で配送するため、stdout → 自動抽出が成立する。ただし「ECS + awslogs」の組み合わせを一文で明記した公式ページは無いため、Step 2 のデプロイ後に `AWS/Logs` namespace の EMF エラーメトリクスで実地確認する。

## 3. 通知経路

- `aws_sns_topic`(例: `vector-alerts`)1 本。全 alarm の ALARM / OK(復旧)action と EventBridge rule target を集約。severity 別 channel 分離はしない(初期は 1 channel)。
- SNS → Amazon Q Developer in chat applications(旧 AWS Chatbot)→ Slack。追加コストなし。Slack workspace の初回 OAuth 承認だけコンソール手動(1 回きり)。
- Slack workspace ID / channel ID は Terraform variable とし、実値は非コミット tfvars で渡す(公開 repo に焼かない)。
- chatbot の API endpoint は ap-northeast-1 に存在しない(us-east-2 / us-west-2 / ap-southeast-1 / eu-west-1 のみ)。channel configuration リソースだけ `region = "us-east-2"` を明示する。設定は account 単位で効き、他 region の SNS topic も購読できる。deploy role の IAM 側は region 条件の無い GlobalInfra 文に `chatbot:*` を置く。
- Terraform リソースは `aws_chatbot_slack_channel_configuration`(hashicorp/aws **v5.61.0 以上**)。`sns_topic_arns` で topic を紐付け、`configuration_name` / `iam_role_arn`(channel 用 role)/ `slack_team_id` / `slack_channel_id` が必須。サービスは Amazon Q Developer へ改名済みだが、API namespace / IAM action(`chatbot:*`)/ Terraform リソース名は旧名のまま。
- `guardrail_policy_arns` は**未指定だと AWS managed `AdministratorAccess` が適用される**ため、読み取り系へ明示的に絞る(通知用途に書き込み権限は不要)。
- SNS subscription の raw message delivery は無効のまま使う(有効化すると Q Developer が処理できない)。
- CloudWatch alarm の ALARM / OK 通知はネイティブ整形(グラフ画像付き。channel role に CloudWatch read 権限が必要)。EventBridge 発は A5 の input transformer 方式で流す。
- Terraform 化できない手動作業(1 回きり): コンソールでの Slack workspace OAuth 認可、Slack 側でのアプリ承認、通知 channel への `/invite @Amazon Q`。workspace ID(`slack_team_id`)はコンソールの Workspace details から取得して tfvars に渡す。

## 4. コスト概算

- カスタムメトリクス 約 22 系列(dispatch_run 3 + age 5 + observation_up 5 + assessment_outcome 5 + ai_provider_exhausted 4)≈ $6.6/月
- alarm 11 本(A1×1, A2×5, A3×1, A4×1, A6×1, A7×1, A8×1)≈ $1.1/月
- SNS / Chatbot / EventBridge: 無料枠内
- 合計 $8/月未満。Logfire 側の削減はなし(trace は残留のため)。

## 5. 実装順序(1 アラートずつ確定 → 実装 → 次へ)

通知経路は全アラートの依存なので最初に敷く。以降は 1 ステップ = 1 アラート(条件の最終確認 + 実装 + 検証)とし、前のステップが本番で生きてから次へ進む。

| Step | 内容 | 変更範囲 |
|---|---|---|
| 1 | 通知基盤: SNS + Chatbot(Slack)+ A7 / A8 alarm + A5 EventBridge rule | Terraform のみ |
| 2 | A1 供給ハートビート: EMF helper + dispatch_run emit + alarm | backend + Terraform |
| 3 | A3 観測死活: observation_up 二重 sink + alarm | backend + Terraform |
| 4 | A2 工程別滞留: age 二重 sink + embedding target 追加 + alarm × 5 | backend + Terraform |
| 5 | A6 AI 枠枯渇: ai_provider_exhausted emit(analysis + agent)+ alarm | backend + Terraform |
| 6 | A4 assessment 失敗率: assessment_outcome emit + alarm | backend + Terraform |
| 7+ | 失敗率の工程展開(1 工程ずつ分母を確定して追加) | backend + Terraform |

- A3 を A2 より先にするのは、A2 が観測の生存を前提とするため(メタ監視を先に敷く)。
- Step 1 / Step 2 の前提事実(Chatbot の Terraform 対応、stopCode 値域とデプロイノイズ除外、EMF 書式)は 2026-08-11 の /research で確定済み(§3・A5・§2 に反映)。
- 手動作業: Slack OAuth 承認(Step 1 の apply 前後に 1 回)。

## 6. Open Questions

- 通知先 Slack channel は既存のものを使うか新設するか(実値は tfvars 側)。
- A2 / A4 / A6 の閾値は初期値であり、本番実測 2〜4 週間後に見直す前提でよいか。
- broker Valkey の maxmemory policy が eviction を許す設定の場合、eviction = queue メッセージの silent loss(実害)なので `Evictions > 0` alarm を 1 本追加する。noeviction なら書き込み失敗として A1 に出るため不要。実装時に policy を確認して判断。
- `vector.audit.dropped` 急増の扱い: 初期アラートからは除外した(実害が間接的)。dashboard spec 側で扱う想定でよいか。
