# ADR-008: pipeline_events — パイプライン監査基盤

> 日付: 2026-06 / ステータス: Accepted (実装済 / 全 11 Stage 横展開済)
>
> 現行契約の SSoT は [docs/observability/pipeline-events-design.md](../observability/pipeline-events-design.md) と
> [docs/observability/pipeline-events-failure-attributes.md](../observability/pipeline-events-failure-attributes.md)。
> 本 ADR は「なぜこの形にしたか」を記録する。

## Context

Vector のニュース処理は taskiq worker 上の **11 Stage** 非同期パイプライン
(dispatch / acquisition / completion / curation / assessment / embedding /
backfill 3 系統 / briefing / trend_discovery) で動く。各 Stage は外部 I/O
(HTTP 取得 / LLM API) と DB 書込を伴い、失敗の種類も「相手サーバ落ち」「LLM の
safety block」「API key 設定漏れ」「DB 制約違反」「想定外バグ」と幅広い。

非同期パイプラインの怖さは **失敗が黙って消える** ことにある。HTTP リクエストと
違い、worker の失敗はユーザーに 500 を返さない。記事が 1 件分析されないまま
落ちても、誰も気づかないまま在庫に埋もれる。本番運用前にまず必要なのは
「どの Stage で・何が・なぜ起き・記事はどう扱われたか」を **後から SQL で
再構成できる** 監査基盤だった。

素朴な実装には 2 つの誘惑があり、両方とも却下した:

1. **業務テーブルに reason 列を生やす** (`articles.extraction_state`,
   `discovered_articles.failure_reason` 等)。状態テーブルが時系列の事実を
   兼ねると、最新状態しか残らず「何度失敗したか」「先週は動いたか」が消える。
2. **監査を制御状態テーブルとして使う** (cron が監査行を読んで再投入を判断する等)。
   監査が制御に使われると、監査は「事実の記録」でなく「次にどうするかの指示」に
   変質し、書き換え圧力がかかって immutable でなくなる。

## Decision (要約)

`pipeline_events` を **append-only / immutable** なイベントログとし、1 行 =
1 イベントで全 Stage × 4 EventType を表現する。失敗の意味論は「発生時点の事実」
だけを焼き、retry 可否・業務副作用・stage-local 詳細は **直交した属性に投影** する。
監査行の組み立ては Stage ごとの `*AuditRepository` (semantic method) に閉じ込め、
業務コードからは観測の関心を排除する。

top-level column (横断検索したい属性のみ) と payload JSONB (stage 固有の詳細
snapshot) を分離する:

```text
id / occurred_at / stage / event_type / outcome_code / retryability
source_id / article_id / error_class / trace_id / payload(jsonb)
```

以下、個別の設計判断とその根拠を記す。

## 設計判断

### 1. 失敗は型付き例外で raise する。成功種別の Outcome に混ぜない

Service が返す `Outcome` (成功・冪等 skip) に失敗 variant を入れる案を却下した。
理由は 3 つで、いずれも具体的な機構の問題:

- **taskiq の retry は raise でしか発火しない**。retryable を return すると
  自動リトライが沈黙する。
- Task 層が `isinstance` で成功/失敗を判定することになり、「処理方針の dispatch」
  という Task の責務が崩れる。
- 「成功で止まる (noise 判定で打ち切り)」と「失敗で止まる」は本質が違う。
  同じ union に入れると Service が「どう進めるか」まで決めてしまい層が越境する。

失敗は `STAGE` / `FAILURE_KIND` / `RETRYABILITY` / `FAILURE_ACTION` を ClassVar
に持つ marker 例外として raise し、Task 層が大枠 (4 marker 程度) で dispatch、
失敗属性の確定は projection に委ねる。

### 2. 成功・skip は業務と同一 tx、失敗は別 session・別 tx で焼く

成功/skip の監査 INSERT は業務 state 更新と **同一トランザクション** に置く。
これで「監査行が焼けた = 業務が確定した」が DB レベルで保証され、片方だけ残る
不整合が構造的に起きない。

失敗時は業務 tx が既に rollback されているため、**別 session・別 tx** で焼く。
監査 INSERT 自体が倒れた場合は呑んで warning に留め、監査の失敗で業務 task を
道連れにしない。最も鋭い適用例は completion の persist crash 経路で、ここを
同一 tx にすると「永続化に失敗した」という監査ごと rollback で消えるという
矛盾が起きる。別 session がそれを断つ。

### 3. 過積載した分類 enum を直交軸へ分解する (投影)

初期は単一の分類 enum に「何が起きたか・retry できるか・記事を消すか」を
詰め込んでいた。これが **2 つの真実** を生んだ。一覧表 (語彙 registry) と
例外型階層が別々に育ち、ズレた。

決定的だったのは **「アラートが事実と矛盾して黙る」** 問題。retry 状態で
category を決める設計だと、intrinsic に直らない例外 (API key 欠落) が attempt
1〜2 では `retryable` と焼かれ、`WHERE category='non_retryable'` のアラートが
最終 attempt まで沈黙する。「直らないのに直る扱い」というラベルの嘘である。

そこで 1 つの enum を直交軸へ分解した:

| 軸 | 列 | 意味 |
|----|----|------|
| 結果種別 | `event_type` | succeeded / skipped / rejected / failed (内容棄却は rejected) |
| 何が起きたか | `outcome_code` | イベントの唯一の code。失敗時は例外の `code` をそのまま焼く |
| retry 可否 | `retryability` | retryable / non_retryable / unknown。**発生時点で intrinsic に確定** |
| stage-local 種別 | `payload.failure_kind` | `terminal_drop` / `recoverable` 等、Stage と組で意味が決まる |
| 業務副作用 | `payload.failure_action` | `drop_article` のみ明示。「保持」は非 action |

retry 上限に到達した事実は intrinsic な retryability ではないので、必要な
Stage が `payload.retry_exhausted` で別途表す。「発生時点の性質 (category)」と
「実際に諦めたか (retry 状態)」を別軸にしたことで、アラートが事実と一致する。

`outcome_code` は例外型の `code` ClassVar/instance 属性から投影される。型を
rename しても `code` 不変なら SQL の連続性が壊れない。一覧表は「型から導出される
表」に格下げした (2 つの真実を 1 つに戻した)。

### 4. 監査は「事実の witness」— 派生事実・採番 PK を焼かない

`pipeline_events` は発生時点の immutable snapshot であって、後の JOIN を楽に
する helper table ではない。この原則から、AI 応答時点で存在しない採番結果
(`assessment_id` 等の自動採番 PK、FK 解決結果の `category_id`) は **事実ではなく
操作的副産物** として payload から排除した。`type(scraper).__name__` のように
定数で情報量ゼロの値も焼かない。成功本文は `articles.original_content` で全文
取得できるので audit に再録しない。

副次効果として、catalog の rename 耐性・cascade delete 耐性・temporal coupling
の解放を得た。「監査が ID を要求するせいで Repository が `tuple[int, int]` を返し
Service が郵便配達人になる」責務混入の連鎖も消えた。

`error_class` (runtime FQN) は残すが、これは forensic 情報であって主契約では
しない。横断検索する属性は top-level、それ以外は payload、という線引き。

### 5. payload の shape を `*AuditRepository` に集約する

Service / Task は `PipelineEventRepository.append()` を直叩きせず、Stage ごとの
semantic method (`append_signal` / `append_drop_article` / `append_failure` /
`append_ready_build_blocked` 等) を呼ぶだけにする。payload 構築・outcome_code
導出・AI raw response の長さ制限・PII 秘匿はすべて Repository に閉じ込める。

これにより観測コードが業務コードを汚さない。例えば curation の Service は
「signal だった」とだけ言えばよく、どの列にどう焼くかを知らない。失敗属性の
投影 (`project_failure`: marker → DB 例外 → catch-all unknown の順で分類) も
1 箇所に集約され、Stage を跨いで一貫する。

PII 対策は構造で行う。`error_message` は `redact_secrets(str(exc))` を通し
長さ制限を掛ける。prompt injection の境界タグを検知した場合の signal (metric +
log) は **監査行を永続化した後** に emit し、「metric +1 だが pipeline_events
行なし」の乖離を防ぐ。

### 6. 記事削除を伴う失敗は provider の明示拒否のみ。FK 切断に耐える payload を残す

記事の物理削除 (`failure_action='drop_article'`) を許すのは、LLM provider が
明示的に拒否した場合 (input rejected = token 超過 / safety、output blocked) に
限る。format 違反や schema 不一致は retryable とし、即削除しない。これは
**「API key 直し忘れで記事が大量削除される」** 事故を避けるための分離で、
直せる失敗で在庫を溶かさない。retry 上限到達分も即削除せず、別 cron が TTL で
物理削除する設計にして、プロンプト改善やモデル切替で過去記事を救済する時間的
余地を残す。

削除と監査は同一 tx で焼くため、`pipeline_events.article_id` FK は
`ON DELETE SET NULL` で切れる。そこで削除に耐える記事識別子を
`payload.target_article_id` に控える。これが無いと「どの記事を消したか」が
削除後に追跡不能になる。

### 7. dispatch / run anchor で「動かなかった週」を可視化する

subtask の集計だけだと、fan-out 入口 (dispatcher) や週次 cron 自体が落ちた
場合に痕跡がゼロになり、「先週 briefing が動いていない」を SQL で検知できない
(沈黙の故障)。そこで dispatch stage は source 単位 outcome と run 単位 outcome
を分けて焼き、週次 run は成功/失敗の anchor を 1 行/週で残す。「無」を「無の記録」
にすることで、動かなかったこと自体が観測可能になる。

## 却下した代替

| 案 | 却下理由 |
|----|---------|
| 業務テーブルに reason 列 (sparse column) | 最新状態しか残らず時系列の事実が消える。状態と監査の責務混在 |
| 監査を制御状態テーブルとして cron が読む | immutable 性が崩れ、監査が「指示」に変質する。制御状態は `*_backfill_exclusions` 等に分離 |
| 単一分類 enum に retry 可否・副作用も詰める | 「アラートが事実と矛盾して黙る」。語彙 registry と型階層が 2 つの真実にズレる (判断 3) |
| 失敗を成功 Outcome union に入れる | taskiq retry が発火しない / Task の dispatch 責務が崩れる (判断 1) |
| 採番 PK / FK 解決結果を payload に焼く | 発生時点に存在しない = 事実でない。rename / cascade に脆くなる (判断 4) |

## 保証範囲 (過大表現しない)

- 本基盤は **dev 環境で全 11 Stage に実装・検証済**。本番は未デプロイのため、
  失敗率改善などの **運用数値はまだ提示できない**。本 ADR が主張するのは
  設計判断であって計測成果ではない。
- 監査は「焼けた事実が正しい」ことを保証するが、**焼き漏らしゼロは保証しない**。
  失敗 INSERT 自体が倒れた経路は warning に留め業務を優先するため (判断 2)、
  監査の網羅性は best-effort。致命的な漏れは Logfire / OTel span を併用して補う
  ([docs/observability](../observability/) 参照)。
- `trace_id` は observability infra 用の optional 列で、失敗属性 projection の
  主契約ではない。処理時間は Logfire / OTel span duration を見る。

## Consequences

- 失敗の横断集計が単純な SQL で取れる (例: `WHERE event_type='failed' AND
  retryability='retryable' GROUP BY stage, outcome_code`)。partial index
  (`ix_pipeline_events_failed`) と GIN index (payload) が集計を支える。
- 新 Stage の追加は `Stage` enum への 1 値追加 + CHECK migration + 専用
  `*AuditRepository` で完結する。失敗投影 (`project_failure`) は共通なので
  Stage 固有なのは marker 例外の ClassVar と payload shape だけ。
- 監査と業務が同一 tx (成功) / 別 session (失敗) という非対称を読者が理解して
  いる必要がある。これは本 ADR と各 `*AuditRepository` の docstring が埋める。
- `pipeline_events` は append-only で増え続けるため、retention purge を
  別 cron (backfill stage と同居) で行う。

## 関連

- 現行契約 SSoT: [docs/observability/pipeline-events-design.md](../observability/pipeline-events-design.md) /
  [pipeline-events-failure-attributes.md](../observability/pipeline-events-failure-attributes.md)
- 実装: `backend/app/audit/` (`domain/event.py` の `Stage`/`EventType`、
  `repository.py`、`failure_projection.py`、`stages/*.py` の per-stage semantic API)
- [ADR-001](001_taskiq_over_arq.md) — taskiq の retry セマンティクス (判断 1 の前提)
- [ADR-004](004_unit_of_work_service_convention.md) — tx 境界の規約 (判断 2 と地続き)
