# Agent run の deadline と結果受理モデル

Status: Draft

この文書は、Agent run の実行モデルについて合意した重要な判断を残す。
詳細な実装計画ではない。実装時はこの契約から小さなスライスを切り、1スライスずつ
テストファーストで進める。

## Problem

現行は、answer generation、Taskiq、stale判定、frontend待機のそれぞれが別の時間を持ち、
「いつまでrunの結果を採用してよいか」が1つの契約になっていない。

また、SSE切断やpollingへの切り替えと、runが完了・失敗・停止したことが混同されている。

## Core decisions

### 1. deadline は結果の受理期限である

- `deadline_at`はworkerの生存判定ではなく、このrunの結果を受理できる最終時刻である。
- 起点は受付時刻`created_at`とし、run作成時に1度だけ`deadline_at`を固定する。
- queue待機も予算に含める。retry、再配送、worker開始、SSE再接続で延長しない。
- 判定はPostgresの時刻で行う。workerやfrontendのwall clockは正本にしない。
- `now < deadline_at`の場合だけdeadline内とし、境界時刻ちょうどは期限切れとする。
  `now`はその場で取得するDB時刻であり、永続化する列ではない。
- runの開始・終端時刻は記録しない。`started_at`・`completed_at`はアプリから読み書きせず、
  DB上の旧列の削除は[別段階で行う](agent-run-time-column-retirement-slice.md)。
- deadlineの長さは60秒とする。現行の150 / 180秒とは別のdomain policyである。

### 2. 結果はDB transitionが勝ったときだけ確定する

- LLMやproviderが応答しただけでは完了ではない。それは未確定のcandidate resultである。
- deadline内に`completed`へのDB transitionが成功し、回答と同じtransactionでcommitされた
  結果だけを採用する。
- `completed`と`deadline_exceeded`はactive runを条件付き更新し、一方だけが勝つ。
- deadline後に返った結果は、workerを停止できたかどうかにかかわらず破棄する。
- worker由来の`completed`、`failed`、`policy_blocked`はdeadline内だけ確定できる。
  deadline後のactive runは`deadline_exceeded`へ収束させる。
- terminal statusは吸収状態とし、遅れたworker、retry、sweeper、cancelが上書きしない。

### 3. run status と worker 生存状態を分ける

- DBでは`queued`と`running`を分けたまま保存する。`active`はこの2状態の総称であり、
  新しい永続statusにしない。
- 時間切れは`deadline_exceeded`とする。`policy_blocked`、意味的失敗、user cancelはこのstatusへ溶かさない。
- workerが死亡しても、それだけでrunを`failed`や`deadline_exceeded`にしない。runはactiveのままとし、
  後続retryまたはdeadline exceeded transitionで収束させる。
- user cancelとdeadline exceeded transitionが競合した場合は、先にDB terminal transitionへ成功した方を維持する。

### 4. 停止は best-effort、不採用は必須とする

- workerは`deadline_at`から残り予算を求め、予算がなければproviderを呼ばない。
- deadline到達後はcoroutineやprovider streamの停止を試みるが、物理停止の成功は正しさの
  前提にしない。
- complete側が遅延結果を拒否するだけでは不十分である。deadlineを超えたactive runに
  `deadline_exceeded`を書くsweeperを必ず持つ。

### 5. SSE と polling は通知経路である

- run statusと確定結果の正本はPostgresとする。
- SSE、再接続、GET pollingは通知と追いつきのために使う。workerの生死を判定しない。
- SSEが切れただけでrun statusを変更したり、表示中のdraftを破棄したりしない。
- frontendはDBの`deadline_exceeded`を確認したときだけ、未確定draftを破棄して時間切れを表示する。
- Redis Streamの10分idleはbrokerの未ACK回収用であり、runのdeadlineや復旧時間にしない。

## Heartbeat / lease

worker heartbeatやRedis leaseは初回実装に含めない。これらはworker死亡をdeadline前に検知し、
早くretryするための最適化である。deadline後の結果不採用を保証する仕組みにはしない。

導入する場合は、二重実行や二重課金を先に防ぐため、少なくとも次を必要とする。

- current attemptを識別するfence。
- retryに必要な残り時間の下限。
- attempt数の上限。

## Daily quota との境界

日次quotaの「1回」を受付、worker開始、provider実行、結果確定のどこで数えるかは、
このdeadline実装と分けて決める。

quota再設計までは、deadline導入が現行の予約・返却ポリシーを暗黙に変えないよう、
その振る舞いを先にテストで固定する。現行の互換境界は次のとおりである。

- `queued`中の取消し・期限切れは予約を返す。
- `running`到達後の期限切れは予約を保持する。

quota再設計は独立した仕様とスライスで行う。

## Implementation workflow

この変更を一度に実装しない。各スライスは、単独で達成条件を説明でき、必要な検証後に
次のスライスへ進める大きさにする。

### Slice order

1. **Consumer compatibility**
   - DBとbackendが`deadline_at`を保存・読取でき、APIとfrontendが`deadline_exceeded`を扱えるようにする。
   - この段階では、productionで`deadline_exceeded`を書き始めない。
2. **Result acceptance and durable abort**
   - terminal transitionをdeadlineでfenceし、`completed`と`deadline_exceeded`の一方だけが勝つようにする。
   - sweeperにより、worker死亡時もactive runを`deadline_exceeded`へ収束させる。
3. **Worker self-stop**
   - 残り予算でworkerの実行を制御し、deadline後のcandidate resultを破棄する。
4. **Legacy clock removal**
   - 新しい契約の動作確認後、旧application timeout、stale判定、frontend local deadlineを
     run outcomeの根拠から外す。

daily quota再設計とheartbeat / leaseは上記と別のスライスとする。

### Test-first rule

各スライスでは、production codeより先に、変更する各層の保証テストを追加する。

1. スライスの`Problem / Evidence / Invariants / Non-goals / Done`を定義する。
2. そのスライスが変更する層ごとに、成功、失敗、deadline境界、必要な競合をテストで固定する。
3. 未実装の仕様が原因で失敗する`intended red`を確認する。
4. そのテストを通す必要十分なproduction codeだけを実装する。
5. 対象テストと影響範囲のcheckが通ったら、そのスライスをPASSとして次へ進む。

受理済みのテストを実装都合で弱めない。各スライスの詳細な対象ファイル、migration手順、テストケースは、
この文書に追記せず、実装開始時にそのスライスのStep Packetとして作る。

## Confirmed deadline

- `RUN_DEADLINE = 60 seconds`
