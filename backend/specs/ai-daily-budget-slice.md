# AI daily budget slice 仕様

Status: Draft(2026-08-14 設計合意ベース。§判断項目に未決あり)

## Problem

rpd 1500/24h の滑り窓 rate limit rule は無料枠時代の名残であり、有料化後の「一日の呼び出し上限」は provider に課される制約ではなく自分が決める支出ポリシーである。現行実装はこの区別を持たず、次の盲点を作っている。

- rpd 超過を gate が bool に握りつぶし(`gate.py` の `except RateLimitExceededError: return False`)、task は `rate_limited` として黙って ack する。どのルールに当たったかの識別は gate 内で消える。
- rpm(block=True)は待つだけで例外を投げないため、gate の `False` の実源は事実上 rpd 枯渇のみ。つまり「今日はもう回復しない」事象を「一時的な混雑」の扱いで処理している。
- 到達は A2(ack で stream を離れ age は 0 リセット)にも A6(provider エラーが発生しないため `ai_provider_exhausted` 不発)にも hold にも映らない。backfill が 30 分ごとに再投入→即 skip→ack の空回りを続け、backfill 日次予算も浪費する。パイプラインが半日停止しても全アラートが green になり得る。

本 slice は「AI 呼び出しの一日の予算」を第一級概念 `ai_daily_budget` として定義し、到達の判定・停止・可視化をこの概念の下に置き、rpd 滑り窓を撤去する。

## 語彙(確定)

| 語 | 意味 | 代表識別子 |
|---|---|---|
| rate limit | 一定時間あたりの速度制御。待つだけで失敗しない(自前 pacing) | `ratelimit:*`, rpm |
| クォータ | 割当者が**別の利用主体**に付与する利用枠。本 slice では扱わない | agent user daily quota |
| **予算** | **支出の責任者が自プロダクトに課す消費上限という政策** | `ai_daily_budget`, `backfill:budget:*` |
| Exhausted 族 | provider から観測したエラーの既存語彙(利用枠・残高の枯渇)。自分の予算側では使わない | `AIProviderUsageLimitExhaustedError` ほか |

規則:

- 述語は Reached。予算は provider 呼び出し前に hard cap として強制され、到達は自分の政策が発動した状態を表す。Exhausted は provider 観測側に予約する。到達イベントの型は `AIDailyBudgetReached`。
- user quota は値を Vector が決めていても、割当者と制約主体が別なので quota である(単位が件数からトークンへ変わっても概念は同じ)。
- 観測出口(stage span の結果値・EMF metric 名・log event)は一字一句同じ `ai_daily_budget_reached` を使い、Logfire と CloudWatch を同一文字列で突き合わせられるようにする。
- 概念名に単位(call / 回数)を焼かない。数える単位は実装詳細であり、将来トークン・金額ベースへ変わっても名前が生き残るようにする。

## 設計

### 概念

- `ai_daily_budget` は provider × model 粒度、UTC カレンダー日、呼び出し回数単位の自主上限。
- 粒度を provider × model にするのは rate limit policy と同じ軸に揃えるため(rpd の等価置換であり、同一 model を共有する stage が 1 カウンタを共有する既存意味論を保存する)。
- 到達状態は予算カウンタから導出する。**hold は立てない**。理由: (1) カウンタという正が既にあり、hold を立てると同じ事実の二重状態になる、(2) hold の責務は「provider / stage 全体の健全性問題」であり、予算到達は健全性問題ではなく政策どおりの停止、(3) UTC 日付ロールでキーが切り替われば何も解除せずに自動回復する。
- hold は provider 障害系(残高切れ=運用者対応、provider 申告の枯渇=条件回復)専用のまま変更しない。hold TTL 6h の意味は「6 時間ごとに 1 attempt 燃やして回復を探査する間隔」であり、この解釈を `stage_hold.py` の docstring に明文化する。

### データ

- Redis key: `ai_daily_budget:{provider}:{model}:{YYYYMMDD}`(UTC、`utc_now()` 基準)。TTL 26h(backfill budget と同じ日跨ぎ猶予)。
- 消費は Lua の check-and-consume(GET / 比較 / INCRBY / EXPIRE を atomic)。`app/queue/helpers/budget.py` の `_LUA_CONSUME_BUDGET` と同型で、消費単位は 1 呼び出し。
- valkey ACL(infra/aws/valkey.tf)に `(~ai_daily_budget:* resetchannels -@all +eval +get +incrby +expire)` を追加する。**新 key を使う app のデプロイ前に infra apply が完了していること**(stage hold key と同じ順序制約)。

### 設定

- `AIDailyBudgetPolicy(provider, model, daily_max_requests)` を AI call spec に optional で持たせる(`rate_limit_policy` と同居するが別概念。未設定 = 予算なし)。
- 初期値: Gemini 生成系(curation の Gemini spec、assessment の Gemini spec)= **1500**。DeepSeek / embedding / agent 系 = 未設定。
- 1500 の意味変化に注意: 滑り窓 → カレンダー日により、日跨ぎ瞬間の 24h 合計は最大 3000 になり得る。実測流量(assessment ~6.8 件/h ≈ 163/day)に対し予算の役割は定常絞りではなく暴走時の課金事故ガードであり、この差は許容する。

### 実行契約 — task(consume、権威)

- 位置: precondition / ready 構築の**後**、rpm pacing の**前**、provider 呼び出しの直前。stale trigger に予算を燃やさせない(既存 gate 配置と同じ理由)。予算 → pacing の順にするのは、予算ゼロの task を pacing で待たせないため。
- check-and-consume で 1 件消費を試みる。granted=0(到達)の場合:
  - EMF `ai_daily_budget_reached{provider, model}` を 1 打点 emit(重複抑制しない。A6 と同じ「発生のたびに素直に打つ」方針)。
  - structlog に到達 log、stage span の結果値は `ai_daily_budget_reached`。
  - provider を呼ばず ack(reraise なし)。
- 到達の emit は判定を所有する consume 境界(budget module の呼び出し側 task ではなく、判定 API 自身が返す事実に基づき task が即 emit)で行い、bool を返して消費者に判断を漏らさない。

### 実行契約 — backfill(read-only、助言)

- 各 stage の backfill run 冒頭で、対象 spec の予算残を GET で読み、0 なら当日の run を skip する(`is_stage_held` チェックと並ぶ短絡)。
- 読むだけで消費も emit もしない。skip は既存の held skip と同型の log を 1 件出す。
- 権威は task 側の consume にある。backfill のチェックは「30 分ごとの再投入→即 skip→ack」の空回りと backfill 日次予算の浪費を止める最適化である。

### rate limit gate の縮小

- 全 policy から rpd rule を撤去する。残る rule は block=True の rpm のみ。
- gate の契約を pacing 専用に縮小する: `acquire()` は待つだけで失敗しない(`-> None`)。bool 返却・task 側の `False` 分岐・`record_rate_limit_gate_skipped`・span 結果値 `rate_limited` を撤去する。
- `RateLimitExceededError` と sliding window の block=False 分岐は、production の使用箇所が gate のみであることを確認済みのため撤去する。`RateLimitRule.block` / `SlidingWindowLimiter` の `block` 引数も全 rule で True になり意味を失うため撤去し、「rate limit = 待つペーシング」を型レベルで真にする。

### 観測・docs 追従

- A6(`ai_provider_exhausted`)は provider 専用のまま変更しない。予算到達は A6 alarm に載せない(§判断項目 1)。
- `specs/observability/cloudwatch-alerting.md` の「一時的 rate limit / gate skip は滞留すれば A2 が拾う」系の記述(L35 / L141 / L171 付近)は事実と異なる(ack により A2 に映らない)ため、予算語彙で書き直す。
- `backend/specs/redis-production-topology.md` の同 metric 別基準(120s/300s)の乖離は本 slice の対象外として記録のみ。

## Invariants

- 予算の消費は Lua check-and-consume のみ。並列 worker 下でも消費合計が `daily_max_requests` を超えない。
- 到達時、AI provider 呼び出しは発生しない(判定は呼び出し前)。
- precondition で skip する task は予算を消費しない。
- 到達状態はカウンタから導出し、hold 等の複製状態を持たない。UTC 日付ロールで自動再開する。
- pacing(rpm)は待つだけで失敗・skip に変換されない。rate limit と予算は独立概念である。
- 観測語彙 `ai_daily_budget_reached` は span 結果・EMF metric 名・log event で一字一句同一。provider 側 Exhausted 語彙と交差しない。
- 認証・認可、既存 alarm(A2 / A6)の定義は変更しない(docs の記述修正を除く)。

## Non-goals

- DeepSeek / embedding / agent 系への予算値の設定(機構は対応、値は置かない)。
- トークン・金額ベースの予算。
- 予算到達の alarm 化(metric まで。alarm は運用してから判断)。
- agent(Q&A)側 user quota・internal search への適用。
- backfill 日次予算(`backfill:budget:*`)の変更。
- 手動 hold 解除 API の再導入。

## Done

- 全 policy から rpd rule が消え、sliding window に block=False 経路が存在しない。
- Gemini 生成系呼び出しが `ai_daily_budget` 1500 の下で動き、到達時: provider 呼び出しゼロ / span 結果 `ai_daily_budget_reached` / EMF 1 打点 / backfill 当日 run skip、がテストで保証されている。
- UTC 日付ロール後に投入・処理が自動再開する(キー切替で検証)。
- valkey ACL が新 key を許可し、Lua の並列・境界挙動が実 Redis 統合テストで検証済み(#136 の流儀)。
- `/check` green。`cloudwatch-alerting.md` の関連記述が新語彙と整合。

## 実装順(PR 分割)

1. **PR-1 infra**: valkey.tf ACL 追加。additive で app 変更なし。デプロイ順序制約(ACL 先行)のため独立させる。
2. **PR-2 backend(expand)**: `ai_daily_budget` module(policy VO / Lua / typed 結果 / EMF)+ Gemini 生成系 spec への設定 + task 配線(予算 → pacing の順、span/EMF)+ backfill 短絡。rpd rule と gate はこの時点では残す(予算が先に止めるため rpd は実質不発になるが、共存期間の安全弁として維持)。テストは red-first。
3. **PR-3 backend(contract)**: rpd rule 撤去 + gate の pacing 専用化 + `RateLimitExceededError` / block 分岐 / `rate_limited` 語彙 / `record_rate_limit_gate_skipped` の撤去。
4. **PR-4 docs**: `cloudwatch-alerting.md` の表現追従。

## テスト観点(test-writer、red-first)

- Lua: 実 Redis で並列 consume の合計が daily_max を超えない / granted=0 境界 / TTL が設定される。
- task: 到達時に provider が呼ばれない・ack される・span 結果と EMF が出る。precondition skip 経路で予算が減らない(順序の保証)。
- backfill: 残 0 で当日 run の投入がゼロ / 残ありで通常動作 / 読み取りがカウンタを変えない。
- 日付ロール: キー切替で再開する(`utc_now` 注入で検証)。
- PR-3 後: pacing が待って必ず進む(既存テストの契約調整)。

## 判断項目(未決)

1. 予算到達の通知形態。推奨 = alarm なし(EMF + ダッシュボード可視化まで)。到達は自分で決めた政策の発動であり、運用者の即時行動を要求しない。毎日到達するようなら予算値か処理量の設計問題として扱う。通知が欲しい場合も A6 には混ぜず別 alarm で立てる。
2. Gemini 初期値 1500 の据え置きで良いか。実測 ~163/day に対し大きな余裕があり、暴走ガードとしての意味付けで据え置きを推奨。
3. assessment の active provider が DeepSeek の場合、Gemini 予算は待機系 spec にのみ効く。DeepSeek(残高従量)に予算値を置くかは Non-goal としたが、課金事故ガードの観点では次の候補。

## 関連

- `backend/specs/agent-user-daily-request-quota-slice.md` — クォータ語彙(agent ユーザー枠)の所有元。本 slice の予算とは別概念。
- `app/queue/helpers/budget.py` — backfill 日次予算。同じ「予算」概念族の先行例(Lua / UTC 日付キー / TTL 26h を踏襲)。
- `specs/observability/cloudwatch-alerting.md` — A2 / A6 の定義。PR-4 で表現追従。
