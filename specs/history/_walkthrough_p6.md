# P6 監査基盤発足 — walkthrough 下書き (整理メモ / WIP)

> 「取り組んだ順序で判断の流れを再構成する」作業の下書き。**プロサ文章化はまだ**。
> 確認済みファクトを箇条書きで蓄積し、後で history ナラティブに昇華する素材とする。
>
> 凡例: ✅ ユーザー確認済 / 🔍 git・コードで検証済 / ❓ 要確認 (私の推定・記憶突合せ待ち)
>
> 注: `specs/history/` は `.gitignore` の例外で **tracked**。本ファイルも追跡対象 (draft の `_` prefix で WIP を示す)。

## スコープと位置づけ

- P6 = 2026-05-01〜08, PR #282–420。**監査基盤 (pipeline_events) 発足期**。
- 開発全体の弧: P0 基盤 → P1 認証/ORM → P2 tx境界 → P3 収集/分析再編 → P4 セキュリティ+フロント → P5 型駆動/Pure DI → **P6 監査基盤** → P7 Stage4/5+厚いReady → P8 Source集約 → P9 監査BC確立+Logfire → P10 本番。
- 早期 P1–P5 は既に `specs/history/` でナラティブ化済。P6–P9 は raw spec のまま = 本作業の対象。

### P6 の並行スレッド (6本が同時進行)

1. Fetcher 大量拡充 (Pattern H/R 約20ソース) — #282–368 散発
2. 🔑 **pipeline_events 監査基盤発足** — #347→#353→#369→#410→#417→#418 ← 主軸
3. red-team 防御 (C1–C9, F系) — #307–423 散発
4. Security/CI hardening Phase 0-2 (osv/semgrep/trivy/schemathesis, non-root, pre-commit, SHA-pin, dev-fallback撤去) — #379–406
5. insights (briefing + weekly-trends) — #304/305/308/315
6. frontend typed API 移行 (hey-api strangler) — #356–365

---

## ⭐ 整理の主構造 (2026-06-02 ユーザー合意) — 三層に分けて読む

P6 の設計判断は性質の違う **3 つの層**が同じ PR/spec に同居していた。混ぜずに分けて読む:

- **(A) 監査設計** — 「**何を・どこに記録するか**」。横断基盤 (table/envelope/witness/同tx/3段防御/raw捕捉)。stage で割らず別立て。→ §A
- **(B) パイプライン設計** — 「**この処理は何をするか / この失敗で何をすべきか**」。ドメイン論理から導く。**2軸 (BC: ニュース取得 / AI分析) を主、stage を従**で整理。→ §B
- **(関係)** — 監査統合は (B) の設計負債を**炙り出した forcing function**。だが炙り出された後の設計は**ドメイン論理で解いた**。監査はその結果の事実を**投影・記録**するだけで、設計を駆動していない。→ §関係

**🔑 核心原則 (quotable):** 「どこで何を記録するか」を問うことは、「その部分の責任とは何か」を問うこと。だが**責任の答えはドメインから導く**——監査の都合では決めない。監査は probe であり witness であって、設計者ではない。

→ **ポートフォリオ価値**: 「監査システムを作った」でなく「**監査統合がパイプラインの設計負債を表面化させ、それをパイプライン設計として正しく解いた。監査は事実を汚さず投影する側に徹した**」。役割の純化そのものが語りになる。

---

## Step 0 — 動機: なぜ監査基盤を作ったか (= §A の前提)

**🔍 fetch_logs の経緯 (素朴なログ → 不十分と痛感 → 退役):**

- 🔍 2026-03-06 (28c33ab7): HN/AlphaVantage クライアント導入時に **fetch_logs を新設** (C-1/C-2 spec)。フェッチを記録する素朴なテーブル。
- 🔍 2026-03-30 (0cfa64bd): fetch_logs に CHECK 制約追加。
- 🔍 2026-04-21 (6c68d7ec): SourceFetchService 切り出し時、**HN 増分フェッチが `fetch_logs.MAX(fetched_at)` を業務状態に流用していた負債**を発見 → Redis へ移動。＝運用ログのはずが業務ロジックに参照されていた。
- 🔍 2026-05-04 (#347): pipeline_events 監査基盤を設計。
- 🔍 2026-05-26 (#637): **fetch_logs を撤去し pipeline_events に一本化** (退役)。

**✅ 痛み (ユーザー証言):** 記事がなかなか流れてこない / 今どうなっているか・どこが悪いか分からず修正がすぐできない / 壊れていても気づかない / 特に rate limit を設定した時それに引っかかっているのか分からない / どの設定を直すべきか分からない。

**🔍 具体的インシデント (種 ADR 背景に明記):** 「過去には **Stage2 が 17 時間停止**していた事実をリアルタイムに検知できず、再現にも数時間を要した」。当時の観測手段は structlog (再起動で消える) / fetch_logs (読み手ゼロ・Stage1のみ) / AI raw response はどこにも残らない / 失敗を SQL 集計する手段が無い、の 4 つしか無かった。
- ❓ 17時間停止が実インシデントだったかは本人記憶でのみ確証可 (掴みに使うなら一言確認したい)。

**✅ 2つの不足への気づき:**
- スコープが狭い: 外部フェッチだけでなく **AI 分析 (curation/assessment/embedding) も記録すべき**。
- 活用されず・むしろ誤用 (運用ログを業務状態 `MAX(fetched_at)` に流用していた)。

→ 監査基盤の動機は **二重の失敗体験**。「最初から完璧な設計を据えた」でなく「素朴なログの不十分さから必要性を学んだ」弧。

---

## §A. 監査設計 (横断基盤) — 「何を・どこに記録するか」

種 = **#347 設計 ADR** (5/3 起票, 841行)。🔍 当時の版を `git show 11f03c29:docs/observability/pipeline-events-design.md` で精読。最小目標 = 「**3ヶ月後の自分が、何が起きていたかを SQL 1本で再構成できる**」。

> 注: ユーザー方針で **ADR は記録の正本でない**扱い。だが「監査として何を記録するか」の原理は ADR に集約されており、§A はそれを正本扱い。per-stage の**処理判断**は §B (spec/git が正本)。

**🔑 第一原理 (quotable):** 「未来の自分(全てを忘れた状態)が、3ヶ月後にこの行 1つを見て何が起きたか再構成できるか」。判定期間を **3ヶ月**と明示 (記憶補完が効かなくなる時点)。

**記録対象を自動的に決めた 2 つの分類体系 (= 恣意でなく原理から導出):**
1. **データのライフサイクル分類 (A〜E)** — 情報を「失われる契機」で分類し責務分担を自動決定。失われると取り返せない情報 (B設定値/Cランタイム文脈/D外部応答) は**捕まえた層が payload に焼く**、後から JOIN で復元可能 (A) は **Repository が補完**。
2. **ランク基準 (S/A/A'/B)** — S=失敗時の構造的詳細、A=FK切断耐性キー、A'=当時値の moment-in-time fact、B=後から導出可能 → **B は載せない**。

**記録の分類規則:** 「outcome_code は分類、件数は payload」。数値・程度で code を分けない (`fetched` 1本、件数は payload で判別、`fetched_empty` は不採用)。

**書込パターンの非対称 (honest trade-off):**
- 成功 / skip パス = 業務と監査を**同 tx でアトミック**。
- 失敗パス = Task 層の except 節で**別 tx 永続化** (`_record_failure_event`)。
- → 「**成功は強整合、失敗は best-effort**」を非対称で明示受容。失敗の D級情報 (AI raw 等) は「業務 raise 後に Task で書く」構造上載らないと正直に明記。

**3段防御 (監査失敗が本業を止めない):** DB(主) → log(副) → 症状検知(最終バックストップ)。第1防御が失敗しても業務 tx は続行。

### §A の機構が #353 PR1 でコードに着地 (🔍 git diff が正本)

- **commit は呼出側責務** (`PipelineEventRepository.append()` は `session.add()` のみ、commit しない) = 「同tx原則」の機構。成功/skip は業務 session に相乗り=アトミック、失敗だけ `_record_failure_event` が `session_factory()` で**新 session・別 tx** commit (業務 rollback に巻き込まれないため)。
- **Repository が source_id を自動補完** (article_id だけ来たら `select(Article.source_id)` 逆引き) = A級情報の Repository 補完が実コード。
- **3段防御が実コード**: 第1防御 (DB) 失敗時は `structlog.exception` で**業務エラーと監査エラーを両方** key 化 (`business_error_*` / `audit_error_*`)。両方同時に失う事故を防ぐ。
- **error_chain 抽出**: `__cause__`/`__context__` を深さ8 + `id()` 集合で循環防止しながら FQN list 化 = S級失敗詳細。
- **段階計画がコードに表出**: `trace_id` は contextvar で常に None (post-v1 Logfire 用に経路だけ確保)、backfill は PR4 へ defer (コメント明記)。

### witness 論理 (記事を消しても監査が事実を保つ) — #410 で結実

DELETE 機構 (= §B-2 のパイプライン判断) に対し、監査側が「記事消失に耐えて事実を残す」ための設計:
- `pipeline_events.article_id` は `ON DELETE SET NULL` (PR1 で設計済の A級保険)。記事 DELETE → CASCADE で extraction 等は消えるが監査行は article_id=NULL で残る。
- payload に `source_name` を常時焼く (FK 切断耐性)。記事消失後も起点ソースを追跡可。
- tx 内順序: **audit INSERT が先、DELETE が後**。DELETE 先行だと source_id 逆引きが NULL になる → INSERT 先行で Article 健在時に source_id 確定。「A級保険を最大化」。

### raw 捕捉 (プロンプトインジェクション検知)

- `ExtractionCall(result, raw_response, prompt_version)` envelope で AI raw を Service まで届ける (Pydantic parse で捨てない)。AI raw response は **Vector のどこにも残らない**極めて貴重な情報。
- input_content は4段階 (raw→truncate→sanitize→rendered) のうち **post-sanitize を hash 対象** (LLM が実際に見た値 + sanitize bug を監査検出可能に)。

### category / code 列 = §B-2 分類学の「投影」

🔑 #417 が追加した `category`/`code` 列は、**§B-2 のパイプライン dispatch 軸を監査に投影したもの**。分類学そのもの (dispatch×origin の判断) は §B-2、列は §A (記録)。spec 自身が「outcome_code に押し込んでいるのではなく**観測軸**として」と投影であることを明言。

### ✅ 監査固有の却下した代替 (= 自己批判型設計・最強のポートフォリオシグナル)

- 「ログだけ」却下: SQL集計/JOIN/長期保持/構造保証が不可能。
- 「試行+結果モデル」却下: taskiq broker が試行を Redis 保持済 → 二重記録は過剰。
- 「二系統 (DB + ログ基盤)」却下: Vector 規模では過剰、structlog fallback で十分。
- 「業務テーブルに reason 列を生やす」案 (状態テーブルが時系列事実を兼ねると「何度失敗したか」が消える) — ❓これは下書きの推定。種 ADR には明記なし、要確認。

### 🔍 種が明示的に post-v1 へ defer したもの (= 後の世代の予告)

1. Logfire instrumentation (B層) + 検知層 — **post-v1** → P9 で実装。
2. 第3防御の SQL 群整備 — 運用優先度で判断。
3. 補正イベント outcome_code prefix 規約 — 運用で必要になったら。

→ **種が「監査(DB/後から) を先、Logfire(runtime/今) を後」と最初から計画していた**。下書きの ❓「rate metric は P9 後付けか」は設計意図として確定: 段階分割は計画通り。

### 痛み → 解決の対応 (🔍 検証済 / 時系列に注意)

痛みは性質の違う **2つの問い**に分かれ、**2層**で解かれた:
- 「後から: 何の段が・なぜ落ちたか」→ **pipeline_events 監査** (P6)。全 stage 1行=1イベント、`event_type='failed'` partial index、stage別 outcome_code。
- 「今: どこで詰まり・どれくらい当たってるか」→ **Logfire runtime 観測** (P9, #629–634)。
- 責務分担は後に `logfire-integration` spec で明文化 (監査=ビジネス事実/数ヶ月、Logfire=運用詳細/数日〜週)。

**rate limit は 3 段構えで解かれた (層が3つにまたがる):**
1. **構造的予防 (= §B-2 パイプライン)**: rate 上限をモデルクラス定義に必須化・起動前検出 (history/ai-provider-model)。設定忘れ自体を防ぐ。
2. **監査 (§A)**: 🔍 `ai_error_rate_limited` outcome_code ([ai_provider_errors.py:71](../../backend/app/analysis/ai_provider_errors.py#L71)) を pipeline_events に焼く。
3. **リアルタイム (Logfire, P9)**: 🔍 `vector.analysis.rate_limit_gate_skipped` Logfire metric を `{stage, model}` 属性付きで ([rate_limit/metrics.py](../../backend/app/analysis/rate_limit/metrics.py))。**どのモデルの上限を上げるべきか**が属性から読める。

---

## §B. パイプライン設計 (監査統合が炙り出した) — 「この処理は何をするか」

**2軸 (BC) 主・stage 従**で整理。各 stage のドメイン概念は 2 つの BC にきれいに割れる。BC が既に load-bearing な設計軸 (コードも `app/collection/` vs `app/analysis/` で物理分離 / 監査の焼き方すら BC で違う [[project_audit_source_name_boundary]])。

### §B-1. ニュース取得 (collection BC) — 失敗の本質 = 接続性 × 概念充足

✅ ユーザーが「**一番問題だった**」と挙げた領域。ドメイン概念 = 外部世界 → ドメインへの取り込み。3 工程:
1. RSS/API 取得 → ドメイン概念へ変換 (acquisition)
2. 完成記事 / 未完成記事に分けて永続化 (acquisition)
3. 未完成記事を HTML 補完で完成させる (completion)

記録すべき失敗の 3 軸 (ユーザー証言): 外部接続の失敗 (接続できたか) / ドメイン概念を満たせない失敗 (どのように) / どの工程か (acquisition / completion)。✅ 「最初はこの工程の切り分けができていなかった」。

#### Stage1 acquisition の判断 (#367 PR1.5 等)
- **`ReadyForArticle` を永続化保証型として1本化** (`FetchedArticle` 廃止統合 / `metadata` 削除)。`FetchedEntry` envelope を新設 (Fetcher が yield する1 entry の運搬箱 = `item` + `metadata`)。
- 収集スコープ宣言 (対象記事の絞り込み) は写像とも変換失敗とも別の第3責務 [[feedback_source_scope_predicate_not_failure]]。

#### Stage2 completion の判断 (#369, 🔍 凍結 spec `git show a43da749:specs/pipeline-events-stage2-design.md` が正本、5 決定)
1. **Outcome shape = 3状態 discriminated union** (`ContentFetched` / `TerminallyDropped` / `TransientlyDropped`)。「nullable では third state を表現できない」→ union。terminal vs transient の区別が後段 skip/救済に必要。
2. **エラーの2チャネル分け**: チャネル1 (例外) = retry 判断が要る `TemporaryFetchError` のみ Service が raise → task が判断。チャネル2 (戻り値) = Service が完結処理したもの (Permanent/empty/promotion Failed/race-lost/成功)。= **retry policy は task の責務、業務完結は Service の戻り値**。
3. payload: `body_length`/`reason_code` 新規、`published_at_source` は **YAGNI 棄却**。
4. **kiq 起動は task 側**、Service は kiq 副作用を持たない (Service テストが broker mock 不要)。
5. **`is_last_attempt` は task が判断**、Service は taskiq retry 概念を知らない → Service は別 method `audit_exhausted` を持つ。

**🔑 履歴 vs 状態の分離 (spec L157, = fetch_logs 誤用の反省を適用):** 「`pipeline_events` は監査ログであって state store ではない」。→ PR2.5 で `discovered_articles` に `terminal_drop_reason`/`published_at_hint` 列を追加し state store を別立て。`MAX(fetched_at)` 誤用と同じ轍を踏まない。
**🔑 変更管理の規律 (spec L228):** PR2 = 観測の整備 (audit 焼くだけ) / PR2.5 = 問題解消 (行動を変える)。「観測を整えてから行動を変えるので、壊れても PR2 の audit で診断できる」。

#### 🔍 自己修正 (P6 内で完結): #370 (PR2.5-A) で `article_urls` テーブルを新設したが **#399 (PR-F) で物理 DROP**。articles.source_url を canonicalize 済み SSoT に格上げ (#381 PR-E) した結果不要に。"作って→使ってみて→畳む" が P6 内で1周。

#### Thread B の深い弧 (= collection BC の責任分離、P6 後に本格化)

✅ ユーザーがもう一つ力を入れた主要タスク。「責任分離はある程度した**つもり**」→ 監査の問い + コードを読む違和感で歪みが露呈 → 何度も再分離。

**🔍 二波構造:**
- **Wave 1 (4/20–26, 監査前)**: 初回の error 型構造化。#76 errors.py / #84 ContentFetchResult sum型 / #107 cause別 result 型 / #142 ContentFetchOutcome union。✅ **当時は「問題ない」と思っていた段階**。
- **監査基盤 (5/4–8, P6)**: pipeline_events 導入。「どこで何を記録するか」を問い始める。
- **Wave 2 (5/13–30, 監査後)**: 失敗型・工程切り分けの**深い再分離**が集中。

**✅ Wave 2 の駆動力は 2 つ (同じ根=責任の混在 の 2 つの現れ):**
1. **監査の問い** — 記録しようとして責任が曖昧だと気づいた = 鋭い forcing function
2. **コードを読む違和感** — 責任が混ざるとコードが見にくい、を少しずつ感じた = 継続的シグナル

**🔍 git が裏付ける:** collection refactor PR = **83 本**、うち失敗型・工程切り分けに踏み込んだもの = **20 本以上**、**5/13–30 に集中**。代表的反復:
- #510 collection BC を Aggregate 軸に再配置し Failed 型を責務分離
- #556 FetchedArticle 変換境界を確立し変換不能 entry を監査可視化 ← 監査可視化が境界を要求
- #574 / #578 失敗を閉じ union に昇格し「失敗証拠を保持」
- #584 失敗を acquisition / completion concern に分離 ← どの工程か
- #586 acquirer を取得(_fetch)/抽出(_extract)の二フェーズに分割
- #599 完成段の失敗型を CompletionRejection 一段に縮約
- #610 read 段の「読めない」失敗を接続エラーから分離 ← 「外部接続失敗」と「ドメイン概念失敗」の分離そのもの
- #687 読取失敗を reason 駆動の自己記述エラーへ実体化

### §B-2. AI分析 (analysis BC) — 失敗の本質 = プロバイダ挙動 × 応答妥当性

ドメイン概念 = ドメイン記事への AI 付加価値。stage 横断で**共有する**判断が多い (= BC を主軸にする根拠)。

#### BC 共有: エラー分類学 (#417, 🔍 凍結 spec `git show cf65d2ee:specs/pipeline-events-error-taxonomy.md` 1069行が正本)

**🔑 引き金はコードの問題 (監査の都合でない):** spec 背景「PR3-a-1 を実装する過程で、Task 層に **10 except 節が縦に並ぶ**状態が出現」「`AnalysisDomainError` 階層が **dispatch 軸を表現していない** — Task 層が処理を分岐できない」。

**🔑 2軸例外階層 (spec L51-53「原則1」) = パイプラインの失敗ディスパッチ設計:**
- **dispatch 軸 (Layer1)** = 「Task 層がどう処理を分岐させるか (処理方針/disposition)」。`isinstance` 1つで取れる。→ DB `category` 列へ投影 (§A)。
- **origin 軸 (Layer2)** = 「失敗の出自・具体的種別」。`type(exc).CODE`。→ DB `code` 列へ投影 (§A)。
- **多重継承で2軸を1型に同居** (左=origin, 右=dispatch marker の MRO 規約)。
- 思想 = **「原因でなく処理方針で分類」**。

**🔑 category 5種 + unknown (categories.py):** `success` / `idempotent_skip` / `retryable` / `non_retryable_drop_article` / `non_retryable_keep_article` (型5種) + `unknown` (DB値のみ、型化しない)。
- **`unknown` を型化しない (spec L903-950):** 「想定外を想定内型にする矛盾」(誰が raise する? 自分が raise する時点で想定内)。dispatch されない型を作る理由がない。DB 6値 / 型5種の非対称。

**`AIProviderError` 9種 (provider.py, BC 横断で共有):** config / request_invalid / insufficient_balance (→KeepArticle) / rate_limited / quota_exhausted / service_unavailable / network (→Retryable) / input_rejected / output_blocked (→DropArticle)。
- **rate limit の写像:** `AIProviderRateLimitedError` = Retryable だが **`INLINE_RETRY=False`** (秒以上の待機は taskiq 即時 retry でなく cron へ)。INLINE_RETRY は Layer1 を増やさず Layer2 の ClassVar で表現 (「Layer1.5 サブクラス案は不採用」)。

**✅ 却下した代替 (自己批判, 多数):** 4種案 (→5種, 下記★) / outcome_code 独立 registry (→型 CODE 投影、二重真実の事故回避) / 失敗を Outcome union に混ぜる (→例外、return だと taskiq retry が走らない) / format 違反を provider Layer2-A に置く (→Stage 別 Layer2-B、「使える応答かは Stage ごとに違う」) / format 違反を DROP にする (→retry 救済が効く) / `AnalysisDomainError` 名 (→`AIProviderError`、「中身が全部 AI インフラ起因なのに Domain は語彙の嘘」) / 短い例外名 (→`AIProvider` prefix で監査自己完結) / category 全行 NOT NULL (→撤回、collection 系に語彙が合わない、レビュー指摘)。判断基準 = 「**次のアクションが違うなら型を分ける**」。

**安全な進め方:** #417 は動作不変、**誰も新型を raise しない** (Phase A = 型追加 + 列追加のみ)。dual-write 後に段階移行。

#### Stage3 curation の判断 (#410, 🔍 ローカル `specs/pipeline-events-stage3-design.md` 1108行が正本)

spec §1 タイトル = 「**大枠の処理分類 (後続処理が違うもので括る)**」。分類軸は「後続処理」= パイプライン論理。

**🔑 大枠5区分 + DELETE 機構 (二値 disposition):**
- 状態は「処理可能 / 不可能」の**二値**のみ。回数で判断せず1回の試行で確定。「不可能 = 分析価値なし = 存在を消す」。
- presence semantic: 成功は `ArticleExtraction` row の**存在**で表現済。DELETE はこれを articles レベルに拡張 (成功=残+INSERT / 一時失敗=残+cron再試行 / 処理不能=DELETE)。

**🔑 Stage 特性で設計を変える (spec §8「Stage 2 との設計差」):** Stage2(HTML)=外部要因で失敗**多発**→複雑な状態管理 / Stage3(AI抽出)=内部LLM限界・**稀**→DELETE で済ます。結語「**HTML 取得時の苦労を Stage 3 で再現しない。Stage の特性に応じて設計を変える**」。同一機構を盲目展開しない。

**🔑 DELETE 対象は内容起因 Permanent 2個のみ:** `ExtractionPolicyBlockedError` (safety) + `ExtractionInputTooLargeError` (context超過)。根拠=「同じ入力で永遠に同じ結果」。**環境起因 Permanent (config/insufficient_balance) は DELETE しない** — 記事の問題でなく、人間が修正後に cron で自然回収。

**🔑 ★最重要連結 (= §B-2 分類学が §B-2 DELETE の暴発を構造的に封じる, spec L242-255/L730-740):**
- ユーザー初期案は **4種** (drop_article まで)。「環境起因 permanent」が欠落。
- 明文化リスク: 「『リトライ不可能』を1つにまとめて記事削除を強制すると、**API key を直し忘れただけで記事が大量に消える**事故が起きうる」。
- 対処 = **5つ目の category `NonRetryableKeepArticle` を分離追加**。DELETE 起動は `non_retryable_drop_article` の `mark_article_unprocessable` だけ、provider 明示拒否2種に厳密化。= **二重の安全弁**。
- → #410 で「強力だが危険な DELETE」を作り、#417 で「その暴発を分類学で構造的に防ぐ」。**第1世代で最も濃い設計スレッド**。

**inline / cron retry 戦略:** Vector は AI quota 律速 → **cron 救済が主軸**、inline (max_retries=1) は network/service_unavailable の短時間 transient 吸収専用。

**🔑 age cutoff の判断 (defer 先, spec §15):** 即 DELETE と別軸。「処理可能だが救済され続けない記事」の累積対策。**時間軸を選んだ理由 = 「ニュースは新鮮さが本質的価値」**。回数判断は LLM 能力依存で恣意的なので不採用。7日 (テックニュースは1週間で価値消失)。

**✅ 却下した代替:** extraction_state カラム / 専用 `article_extraction_failures` テーブル / extraction_noises 意味論拡張 / 回数判断・backoff (恣意的) / 複雑な永続失敗管理 (YAGNI) / `ExtractionParseError` 新設 (ProviderError で吸収) / reason_code 二重表現 / inline retry 中間失敗の監査 (最終結果だけ載せる)。
**defer:** 救済 cron + 7日 DELETE cron + age cutoff は PR3-a-2 へ (#410 には未実装)。`AnalysisDomainError→AIProviderError` rename は PR3.5 へ。

#### Stage4 assessment の判断 — ❓ 未深掘り
- 投資判定 (category/topic/impact)。KIND-based ACL / scope外 = rejected (memory より)。詳細は P7 (#432–467 厚いReady転換) と合わせて要確認。

#### Stage5 embedding の判断
- analysis テキストから vector 生成。**raw I/O 捕捉は不要** (入力は analysis に永続、出力は数値 vector で injection 観点無関係) = §A の raw 捕捉が stage で要否を変える根拠。

---

## §関係 — 監査統合 = forcing function (設計の駆動者ではない)

監査ポイントを設計する作業が、(B) パイプラインの設計負債を**炙り出した**。だが炙り出された後の設計は**ドメイン論理で解いた**。一次資料に残る具体例:

- **#369 (collection)**: spec 背景「(audit を入れる) その過程で『**再試行すべきか否かの区別が現状無い**』ことが判明 (404 で死んだ URL も毎 cron 再試行)」→ terminal/transient の区別を**ドメイン**(retry 価値)で設計。
- **#417 (analysis)**: spec 背景「Task 層に **10 except 節が縦に並ぶ**」「dispatch 軸を表現していない」→ 2軸例外階層を**ドメイン**(後続処理)で設計。
- **#347 (種)**: Outcome 責務純化原則 = 「Service 戻り値は次段に渡す価値があるもののみ、観測値は監査へ焼く」。これが Outcome を「型ベース dispatch passport」に純化し、collection の型階層リファクタ (PR1.5) を要求 = §A と §B の接合点。

→ **監査は probe (露呈させる) であり witness (事実を残す) であって、設計者ではない**。設計の答えは常にドメインから。

---

## 進化の弧 (第1世代 → P9) — ❓ 次の深掘り対象

第1世代 (P6) は成熟していたが、以下が後に洗練/地層化する:

- **2軸 `category/code` → `concern/mechanism`**: 🔑 直交2軸の**起点は P6 #417** (memory `project_completion_audit_spec`/`project_audit_stage_rename` の「2軸化=P9」は不正確、P9 は再命名・洗練)。#417 が「event_type を残すか廃止するか」を **PR3.8 へ持ち越し** (spec L358-366)。
- **語彙の地層 rename**: source_fetch→acquisition / content_fetch→completion / extraction→curation / classification→assessment (#615/#616)。module `observability`→`audit`。
- **source_name 撤去**: collection=焼く / analysis=foreign で撤去 (#683) [[project_audit_source_name_boundary]]。
- **成功 body_head / scraper_class 撤去** [[project_completion_audit_spec]]。

---

## 第1世代 PR マッピング + 正本ソースマップ (🔍 git 確定, 2026-06-02)

| PR | commit | 内容 | 第1世代 spec 正本 | 当時の語彙 |
|---|---|---|---|---|
| #347 | 11f03c29 | 設計 ADR (841行) | `docs/observability/pipeline-events-design.md` (ADR扱い注意) | `app/observability/` |
| #352 | 5c7f46cc | Stage1 signal/noise relevance フィルタ | — | extraction 段の門番 |
| #353 | 25b117c5 | PR1: pipeline_events 導入 + Stage1 統合 | (spec無) → **コード diff** | source_fetch |
| #367 | 26b12c0a | PR1.5: Fetcher 戻り型整理 + metadata observation | (collection 型階層) | ReadyForArticle 1本化 |
| #369 | 1e869b1d | PR2: Stage2 統合 + ContentFetchService 切出 | `git show a43da749:specs/pipeline-events-stage2-design.md` (262) | content_fetch |
| #370–399 | — | PR2.5-A〜F: 3テーブル正規化 (article_urls #399 で DROP) | — | — |
| #410 | 47e4879b | Stage3 監査統合 + DELETE 機構 | ローカル `specs/pipeline-events-stage3-design.md` (1108, 未commit) | extraction |
| #417 | cf65d2ee | PR3.5-a/b: エラー分類学 (category/code 列) | `git show cf65d2ee:specs/pipeline-events-error-taxonomy.md` (1069) | — |
| #418 | e9853384 | PR3.5-c: ExtractionAuditRepository 集約 | `git show 848628fc:specs/pipeline-events-stage3-extraction.md` (457) | — |

**🔑 方法論 (2026-06-02 確定):** ユーザー方針 = ADR は正本でない / 判断は spec に書いた / 順序と進化は git で判断。🔍 `specs/` は **#620 (P9, 2a437769) で初めて gitignore 化** (`.gitignore:92` `specs/*` + 例外 `!specs/history/`)。それ以前 = 第1世代の全期間は **specs/ が git 追跡されていた** → 第1世代 spec は当時の姿で git に凍結、`git show <commit>:specs/...` で正本が取れる。語彙が上書きされた Stage1/2 でも第1世代の判断が完全復元できる。

---

## 要確認リスト (open questions)

- [x] ✅ 三層 (監査設計 / パイプライン設計2軸 / 関係) に再編成 (2026-06-02 合意)
- [x] ✅ パイプライン設計は 2軸 (collection BC / analysis BC) 主・stage 従で整理 (2026-06-02 合意)
- [x] ✅ rate metric の P9 は「後付け」でなく**設計通りの段階分割** (種が Logfire を post-v1 へ defer)
- [x] ✅ 第1世代の判断は specs (git凍結) + git diff から復元。ADR は正本でない
- [x] ✅ 「2軸化 = P9」は不正確。起点は P6 #417 → memory 注記要
- [ ] **Stage4 assessment の判断**が未深掘り (§B-2)。P7 厚いReady転換 (#432–467) と合わせて要確認
- [ ] #418 (ExtractionAuditRepository 集約) 未深掘り。払拭リファクタで判断密度は低めの見込み (helper を I/O 無し化 / payload shape を Repository に閉込) → 深掘り要否を判断
- [ ] 種 ADR 背景「Stage2 17時間停止」は実インシデントか (掴みに使うなら確認)
- [ ] 「業務テーブルに reason 列を生やす案を却下」は下書きの推定、種 ADR に明記なし — 実検討したか
- [ ] 次の深掘り候補: (a) 進化の弧 (category/code → concern/mechanism, 語彙 rename, source_name 撤去) / (b) §B-1 Thread B の弧 (P8–P9 Source集約) / (c) §B-2 Stage4 assessment

## 検証に使ったソース

- git log (#282–420, fetch_logs/FetchLog lifecycle), `git show` 凍結 spec (#369/#417 spec, #347 ADR)
- ローカル `specs/pipeline-events-stage3-design.md` (#410)
- backend/app/audit/ (event.py / repository.py / failure_projection.py / stages/curation.py), 凍結コード (#353/#369/#410/#417)
- backend/app/analysis/ai_provider_errors.py, rate_limit/{gate,metrics}.py
- docs/observability/pipeline-events-failure-attributes.md
- specs/ (7 サブエージェント精読), specs/history/ai-provider-model.md
