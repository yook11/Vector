# pipeline_events Stage 3 (extraction) 統合設計

## 背景

`pipeline_events` 監査基盤の Stage 3 (extraction) 統合 PR (PR3-a) を着手するに
あたって、設計議論で確定した内容と未確定の論点を記録する。

PR3-prep (#397 merged) で **Prompt class 化 + call signature hash による
prompt_version** の土台はできている。本 spec は監査 event 焼付と失敗 / 救済の
扱いを詰める段階。

## 1. 大枠の処理分類 (後続処理が違うもので括る)

監査の「何を記録するか」は 2 階層に分ける:

| 階層 | 表現 | 安定性 | 役割 |
|---|---|---|---|
| 大枠 (Why) | 例外型 / Outcome variant | 高 | 後続処理の分岐 |
| 詳細 (How) | reason_code (str) / error_chain / error_message | 低〜中 | 集計・調査 |

### 大枠 5 区分

| # | 大枠 | 表現 | 後続処理 |
|---|---|---|---|
| 1 | 成功 | `Outcome.Success` (例外なし) | articles 永続化 + Stage 4 dispatch |
| 2 | 冪等 skip | `Outcome.SkippedAlreadyDone` (例外なし) | 何もせず正常 return |
| 3 | Retryable (Transient) | `ExtractionTransientError` 系 | inline retry → 上限到達で大枠 4 へ降格 |
| 4 | Permanent (Terminal) | `ExtractionPermanentError` 系 | 失敗を確定 + 監査 + 救済対象登録 |
| 5 | Unknown | その他 `Exception` | 大枠 4 と同じ扱い、ただし区別 |

### Retry 戦略 — Inline (taskiq) と Cron (out-of-band) のハイブリッド

| 戦略 | 対象 | 理由 |
|---|---|---|
| Inline retry (max_retries=1) | `NetworkError` / `ProviderError` (5xx + parse 失敗) | 短時間 (秒) で回復する純粋 transient |
| Cron 救済 (主軸) | `RateLimitError` / `DailyQuotaExhaustedError` / `UnclassifiedError` / その他 transient | 時間ギャップが救う、worker resource を解放 |
| Skip (retry なし) | `InvalidInputError` | provider が入力を拒否、同入力で再試行する意味がない |
| 救済不可 (人間対応) | `ConfigurationError` / `InsufficientBalanceError` | 環境問題、cron で残し人間が config 修正 |
| **DELETE 対象** | `ExtractionPolicyBlockedError` / `ExtractionInputTooLargeError` | 内容起因 Permanent、articles 削除 |

Vector の制約 (AI quota 律速、新着記事の流量) では **cron 救済が主軸**、inline は
短時間 transient の吸収専用。

## 2. outcome_code 語彙 — 二層設計の上層

### 設計原則 (二層設計)

`pipeline_events` には **既に top-level column `outcome_code: String(60) NOT NULL`** が存在し、PR1/PR2 で各 Stage が独自語彙で使っている。Stage 3 もこの既存メカニズムに乗る。

`reason_code` を payload に追加しない (二重表現を避ける)。AI が返す raw 情報は payload (`error_chain` / `error_message` / `ai_raw_response`) で全保存し、`outcome_code` は **集計・dispatch 用の安定軸** として上層に乗せる。

| 層 | 役割 | 形式 |
|---|---|---|
| outcome_code (column) | 集計・dispatch 用の安定軸 | 13 個の業務語彙 (String 60) |
| payload.error_chain | 例外型 FQN チェーン | list[str] |
| payload.error_message | 例外メッセージ raw | str (2KB) |
| payload.ai_raw_response | LLM 応答 raw | str (2KB) |

→ outcome_code に押し込んでいるのではなく、**観測軸** として 13 個。生情報は payload に並列保存。

### 概念ベース (provider-agnostic) 方針

既存 `_translate_error` が provider 固有 SDK 例外 (`google.genai.errors.*` / `openai.error.*`) を `AnalysisDomainError` 階層に翻訳する **抽象化境界** として既に機能している。outcome_code はその上に乗り **provider-agnostic な業務語彙** を採用する。

provider 詳細は payload で完全復元可能:
- `payload.ai_model` → どの provider か
- `payload.error_chain` → 実装上の例外型 FQN

### 語彙 (13 個)

既存 `AnalysisDomainError` 階層と 1:1 対応 + 新設 2 個 + Vector 業務語彙 3 個。

| # | outcome_code | event_type | 既存例外型 | 大枠 | 後続処理 |
|---|---|---|---|---|---|
| 1 | `extracted` | SUCCEEDED | — | 成功 | 永続化 + Stage 4 dispatch |
| 2 | `skipped_already_extracted` | SKIPPED | — | 冪等 | 何もしない |
| 3 | `skipped_invalid_input` | SKIPPED | `InvalidInputError` (既存) | skip | 何もしない (retry なし) |
| 4 | `ai_error_blocked_by_policy` | FAILED | `ExtractionPolicyBlockedError` (新設) | Permanent (内容) | **articles DELETE** |
| 5 | `ai_error_input_too_large` | FAILED | `ExtractionInputTooLargeError` (新設) | Permanent (内容) | **articles DELETE** |
| 6 | `ai_error_provider` | FAILED | `ProviderError` (既存、5xx + parse 失敗統合) | Transient | cron 再試行 |
| 7 | `ai_error_rate_limited` | FAILED | `RateLimitError` (既存) | Transient | cron 再試行 |
| 8 | `ai_error_daily_quota_exhausted` | FAILED | `DailyQuotaExhaustedError` (既存) | Transient | cron 再試行 (24h 後自然回復) |
| 9 | `ai_error_network` | FAILED | `NetworkError` (既存) | Transient | cron 再試行 |
| 10 | `ai_error_config` | FAILED | `ConfigurationError` (既存) | Permanent (環境) | articles 残す (人間対応) |
| 11 | `ai_error_insufficient_balance` | FAILED | `InsufficientBalanceError` (既存) | Permanent (環境) | articles 残す (人間対応) |
| 12 | `unclassified_error` | FAILED | `UnclassifiedError` (既存) | Transient | cron 再試行 |
| 13 | `unexpected_error` | FAILED | その他 `Exception` | Unknown | cron 再試行 |

### 設計上の重要ポイント

#### 1. parse 失敗は `ai_error_provider` に統合

リサーチで判明: 既存実装では Pydantic ValidationError (LLM 応答 schema 違反) は `_translate_error` で `ProviderError` にマップされる。新設 `ExtractionParseError` は不要、既存 `ProviderError` で吸収。

両方とも **後続処理が同じ** (transient + cron retry) なので統合して問題ない。schema 違反の独立観測が必要になったら error_chain で `pydantic.ValidationError` を WHERE 条件にすれば SQL で取れる。

#### 2. DELETE 対象は内容起因 Permanent のみ (2 個)

`ai_error_blocked_by_policy` と `ai_error_input_too_large` は **同じ入力で永遠に同じ結果になる** ため DELETE 対象。これらの判定は新設 `ExtractionPolicyBlockedError` / `ExtractionInputTooLargeError` で行う。

ニュース記事においてこれらの事象は実用上ほぼ発生しない (safety filter に引っかかる記事は稀、context window は CONTENT_MAX_LENGTH=20,000 文字で構造的に防止済み)。**複雑な永続失敗管理を構築しない** という設計判断はここに依拠。

#### 3. 環境起因 Permanent (2 個) は articles 残す

`ai_error_config` (API key 不正等) と `ai_error_insufficient_balance` (DeepSeek 残高 0) は **記事の問題ではない** ので DELETE しない。人間が config を修正したら次回 cron で自然回収される。

#### 4. catch-all で網羅性を担保

`unclassified_error` (既存 `UnclassifiedError` 例外) と `unexpected_error` (その他全 `Exception`) で **未マッピング全部** を受ける。新類型が頻発したら error_chain を見て後付けで promote (新 outcome_code 追加は schema migration 不要)。

### 前提とする refactor (PR3.5 で実施、PR3-a の scope 外)

`AnalysisDomainError` という名前は **実体がほぼ全部インフラエラー** で名前と乖離している。PR3.5 で `AnalysisDomainError → AIProviderError` (もしくは類似名) にリネームする予定。本 PR (PR3-a) では既存名のまま参照し、リネーム後も outcome_code 語彙は無変更。

詳細は `app/analysis/classification/service.py:138` の `unknown category slug` を `ProviderError` で流用している事故 (retry policy 誤判定) も含めて PR3.5 / PR-Future で扱う。

## 3. 例外階層 — 既存を尊重、新設は 2 個のみ

### 既存階層 (PR3-a では触らない)

リサーチで判明した既存 `AnalysisDomainError` 配下の例外型 (PR3.5 でリネーム予定):

```
AnalysisDomainError (既存 base、PR3.5 で AIProviderError 等にリネーム予定)
├── InvalidInputError                # 既存、provider が入力を拒否 → SKIPPED
├── ConfigurationError                # 既存、環境起因 Permanent (articles 残す)
├── DailyQuotaExhaustedError          # 既存、環境起因 Permanent (articles 残す)
├── InsufficientBalanceError          # 既存 (DeepSeek 402)、環境起因 Permanent
├── ProviderError                     # 既存、5xx + Pydantic ValidationError 統合
├── NetworkError                      # 既存 (Timeout / Connection / OSError)
├── RateLimitError                    # 既存 (429)
└── UnclassifiedError                 # 既存、catch-all
```

### 新設例外 (2 個のみ)

```
+ ExtractionPolicyBlockedError ★NEW   # safety / recitation / etc → DELETE
+ ExtractionInputTooLargeError ★NEW   # context window 超過 → DELETE
```

これらは **内容起因の Permanent**。既存階層の親クラスは PR3.5 のリネームと整合性を取るため **本 PR では決め打ちしない** (Stage 3 専用の独立例外として `Exception` 直下、tasks.py の except 順で先に捕まえる)。PR3.5 で `AIProviderError` 階層が確定後に親クラスを再配置する。

### 新設例外の役割

- `ExtractionPolicyBlockedError`: Gemini SDK の `finish_reason` が
  SAFETY / RECITATION / BLOCKLIST / PROHIBITED_CONTENT / SPII の時に raise。
  `raw_response` (取れたら) と `prompt_version` を保持。
- `ExtractionInputTooLargeError`: context window 超過 (実用上発生しない
  が型として定義)。SDK の `InvalidArgumentError` の特定パターンから翻訳。

### Extractor 内の判定ロジック (新設)

```python
async def _call_api(self, prompt: str) -> ExtractionCall:
    response = await self._client.aio.models.generate_content(...)
    candidate = response.candidates[0] if response.candidates else None
    finish_reason = candidate.finish_reason if candidate else None

    # 内容起因 Permanent — 新設例外を raise
    if finish_reason in (FinishReason.SAFETY, FinishReason.RECITATION,
                         FinishReason.BLOCKLIST, FinishReason.PROHIBITED_CONTENT,
                         FinishReason.SPII):
        raise ExtractionPolicyBlockedError(
            finish_reason=finish_reason,
            raw_response=response.text or None,
            prompt_version=GeminiExtractionPrompt.VERSION,
        )

    # parse 失敗 — 既存 ProviderError 経路に流す (新設しない)
    parsed = response.parsed
    if not isinstance(parsed, ExtractionResult):
        raise ProviderError(
            f"Gemini returned unparseable response (text={response.text[:200]!r})"
        )

    return ExtractionCall(
        result=parsed,
        raw_response=response.text or "",
        prompt_version=GeminiExtractionPrompt.VERSION,
    )
```

入力過大は `_translate_error` を改修し、`InvalidArgumentError` の特定パターン (例: "exceeds context length") を `ExtractionInputTooLargeError` に翻訳する経路を追加。

### tasks.py での例外捕捉順 (重要)

新設 2 個は **既存例外より先に catch** する必要がある (継承関係に依存しない独立例外として定義するため):

```python
try:
    await service.extract(article)
except ExtractionPolicyBlockedError as exc:
    # outcome_code = "ai_error_blocked_by_policy" → DELETE
except ExtractionInputTooLargeError as exc:
    # outcome_code = "ai_error_input_too_large" → DELETE
except InvalidInputError as exc:
    # outcome_code = "skipped_invalid_input" → 何もしない (retry なし)
except ConfigurationError as exc:
    # outcome_code = "ai_error_config"
except InsufficientBalanceError as exc:
    # outcome_code = "ai_error_insufficient_balance"
except DailyQuotaExhaustedError as exc:
    # outcome_code = "ai_error_daily_quota_exhausted"
except RateLimitError as exc:
    # outcome_code = "ai_error_rate_limited"
except NetworkError as exc:
    # outcome_code = "ai_error_network"
except ProviderError as exc:
    # outcome_code = "ai_error_provider"
except UnclassifiedError as exc:
    # outcome_code = "unclassified_error"
except Exception as exc:
    # outcome_code = "unexpected_error"
```

PR3.5 後は新設 2 個も `AIProviderError` 階層に配置し、3-tier dispatch (SkipReason / TransientFailure / EnvironmentFailure) で簡素化される。本 PR ではこのフラットな except 列挙で OK。

## 4. 監査 event 焼付の規律

### 同 tx 焼付 (成功 / skip)

`ExtractionService` 内で business state (article 永続化) と監査 event INSERT を
同一 transaction で commit。PR1/PR2 (`IngestionService` / `ContentFetchService`)
の `_record_event` match pattern を踏襲。

### 別 tx 焼付 (失敗)

`tasks.py` の except 節で `_record_failure_event` を呼ぶ。Service 側で
exception が raise された時点では Service 側の tx は rollback されているので、
別 session で監査 event を INSERT。`build_failure_payload` (既存) を流用し
`error_chain` / `error_message` を埋める。

### Inline retry 中の中間失敗は監査に書かない

retry 経過は ops log で観察。監査テーブルは「最終結果」(success / skipped /
permanent failed) だけで膨らませない。

## 5. AI client 戻り値の envelope 化

`BaseExtractor.extract()` の戻り値を新規 dataclass に変更:

```python
# app/analysis/extraction/extractor/envelope.py
@dataclass(frozen=True, slots=True)
class ExtractionCall:
    result: ExtractionResult
    raw_response: str        # SDK 戻り値 response.text (parse 前 raw)
    prompt_version: str      # 使った Prompt class の VERSION (call signature hash)
```

理由:
- `ai_raw_response` を Service 層まで届ける配送容器が要る (Pydantic parse で
  捨てない)
- Service が Prompt class を直接 import せずに済む (envelope に同梱)
- 将来拡張 (latency / token usage) が non-breaking
- Stage 4 / 5 にも同形 (`ClassificationCall`) で横展開可能

`_call_api` (provider 固有層) で envelope を組む。`response.parsed is None`
の場合は **既存 `ProviderError` 経路** に流す (新設例外を作らない、§3 参照)。
finish_reason が SAFETY/RECITATION 等の場合のみ新設 `ExtractionPolicyBlockedError` を raise。

## 6. 救済 cron 設計

### Schedule

```
worker-analysis-recovery (新設):
  schedule: 30 分間隔 (or 1 時間)
  対象: extraction が pending かつ最新 pipeline_event が retryable な reason_code
        (具体的判定 SQL は §7 参照)
  処理: ReadyForExtraction を組み直して extract_content.kiq() に再投入
```

### 救済 policy

**回数判定 / backoff 計算なし**。「articles に存在して article_extractions が
無い」を全部拾う。

- 処理不能な article は既に DELETE されている → cron は永遠ループしない
- 処理可能な article は何度試しても OK (quota 浪費を避けるため schedule 周期は
  30 分〜1 時間で調整)
- 環境問題 (config error) は人間が直したら次回 cron で自然回収

### 救済自体の監査

cron task が再投入した記事数を `dispatch` payload で焼付。各救済試行は通常の
`extraction` event として記録される。

```sql
-- 救済の試行回数 (article_id 別)
SELECT article_id, COUNT(*) FROM pipeline_events
WHERE payload->>'kind' = 'extraction'
GROUP BY article_id;

-- DELETE された記事の最後の状態 (article_id NULL のもの)
SELECT
  payload->>'source_name' AS source,
  outcome_code,
  COUNT(*)
FROM pipeline_events
WHERE article_id IS NULL
  AND payload->>'kind' = 'extraction'
  AND outcome_code IN ('ai_error_blocked_by_policy', 'ai_error_input_too_large')
GROUP BY 1, 2;
```

## 7. 失敗状態の永続化方法 — 確定: DELETE 方式

### 設計判断 (2026-05-07 確定)

**処理不可能と判定された article は `analyzable_articles` から DELETE する**。

哲学:
- 状態は「処理可能 / 不可能」の二値しか存在しない
- 回数で判断しない (1 回で判定)
- 不可能 = 分析価値なし = 存在を消す

### 現状の発見 (前提)

`ArticleExtraction` テーブルには **status カラムが存在しない**。
extraction の成功状態は **row の存在 (presence) で表現**:

- 成功 → `article_extractions` row が INSERT される
- 失敗 → row が無い (article 側に何もマークされない)

DELETE 方式はこの presence semantic を analyzable_articles レベルに拡張:

- 成功 → `analyzable_articles` row 残る + `article_extractions` row INSERT
- 処理可能な一時失敗 → `analyzable_articles` row 残る + `article_extractions` row なし → cron 再試行
- 処理不可能 → **`analyzable_articles` row 削除** = 永遠に対象外

### 実装の前提条件 (確認済)

| 項目 | 状態 |
|---|---|
| `pipeline_events.article_id` の DELETE 挙動 | `ondelete="SET NULL"` ★A 級保険として PR1 で設計済 |
| 関連テーブルの CASCADE | `article_extractions` / `extraction_noises` / `article_analysis` 全て CASCADE で綺麗に消える |
| URL dedup | `exists_by_source_url` で機能。削除 URL が再 fetch される可能性は理論上あるが、Vector は新着のみ取得なので実用上低頻度 |
| audit 保全 | `pipeline_events` に `source_name` (FK 切断耐性 field) があり、`article_id` NULL でも追跡可 |

### 採用しない選択肢

- **articles に extraction_state カラム追加** → presence + state の二重表現で矛盾を招く
- **新規 `article_extraction_failures` テーブル** → schema 増、pipeline_events と二重
- **`extraction_noises` の CHECK 緩和 + 意味論拡張** → 既存意味論を変える、テーブル間の責務を曖昧に

### 採用する設計

```
内容起因 Permanent (policy blocked / input too large):
  1. tasks.py の except 節で監査 event を INSERT
     (reason_code="ai_error_blocked_by_policy" or "ai_error_input_too_large")
  2. articles row を DELETE (CASCADE で関連 row も消える)
  3. pipeline_events の article_id は SET NULL になり、source_name で追跡可能

処理可能な失敗 (parse_failed / rate limit / network / etc):
  1. tasks.py の except 節で監査 event を INSERT
  2. articles row はそのまま
  3. cron が pending な article (= article_extractions が無い) を拾い再試行

環境起因 Permanent (config error):
  1. 監査 event を INSERT
  2. articles row はそのまま (人間が config 修正後、cron で自動回収)
```

### 救済 cron の SQL (極めて単純)

```sql
-- 救済対象 = articles 存在 かつ extraction 未完
SELECT a.id
FROM analyzable_articles a
LEFT JOIN article_extractions ae ON a.id = ae.article_id
WHERE ae.id IS NULL
  AND a.created_at > NOW() - INTERVAL '7 days';  -- 古すぎる pending は対象外 (任意)
```

回数判定なし、reason_code 判定なし、backoff 計算なし。
**「articles に存在して article_extractions が無い = 拾う」だけ**。

## 8. 設計哲学のまとめ

### 状態は「処理可能 / 不可能」の二値

回数で判断しない。1 回の試行で確定する。

- **処理可能** → articles を残す → cron が拾う (時間で解消するもの)
- **処理不可能** → articles を DELETE (内容として処理不能なもの)
- **環境問題** → articles を残す (cron 任せ、人間が修正したら自動回収)

### 「処理不可能」が当てはまるのは parse_failed のみ

実用上、Gemini が parse 失敗を返す記事はほぼ存在しない (= ほとんどのニュース
記事は LLM が schema に従って解釈できる)。
なので **DELETE が走る頻度は極めて低い**。複雑な永続失敗管理を構築する
価値がない、というのが今回の設計判断。

### Stage 2 (HTML 取得) との設計差

| Stage | 失敗の性質 | 設計 |
|---|---|---|
| Stage 2 (HTML) | 外部要因 (paywall / JS / 404) で多発 | pending_html_articles テーブル等で複雑な状態管理 |
| Stage 3 (AI 抽出) | 内部 LLM 能力の限界、稀 | DELETE で済ます、状態管理不要 |

HTML 取得時の苦労を Stage 3 で再現しない。**Stage の特性に応じて設計を変える**。

## 9. PR 分割案 (scope が大きいため 2 段階)

### PR3-a-1: 監査統合 + DELETE 機構

scope:
1. 新設例外 2 個: `ExtractionPolicyBlockedError` / `ExtractionInputTooLargeError` (内容起因 Permanent、`raw_response` / `prompt_version` を保持)
2. `ExtractionPayload` には `reason_code` を **追加しない** (既存 `outcome_code` column で表現)
3. AI client envelope 化 (`ExtractionCall(result, raw_response, prompt_version)`)
4. `ExtractionService` 同 tx 焼付 (成功 / skip イベント、`outcome_code` 1〜3 番)
5. `tasks.py` except 節改修:
   - inline retry を `NetworkError` / `ProviderError` のみに絞る (max_retries=1)
   - 各例外 → outcome_code への mapping 確定 (§3 except 順を実装)
   - 監査 event 書込み (失敗パス、別 tx)
   - `ai_error_blocked_by_policy` / `ai_error_input_too_large` の 2 outcome のみ **articles DELETE を実行**
6. Gemini extractor の `_call_api` に finish_reason 検査ロジック追加
7. `_translate_error` 改修: `InvalidArgumentError` の context length パターンを `ExtractionInputTooLargeError` に翻訳
8. payload 組立 helper (`base_extraction_payload_fields`、§12 参照) 新設
9. ADR 改訂: outcome_code 語彙表 + DELETE 規律明文化

成果: 失敗が pipeline_events に確実に記録 + 処理不能 article は DELETE で
消える。手動 CLI (`re_extract_all.py`) で当座運用可能。

### PR3-a-2: 救済 cron + 古い記事 DELETE cron

scope:
1. 救済 cron task 新設 (`recover_failed_extractions`)
   - 7 日以内の articles で extraction 未完のものを再投入
2. `re_extraction_service.py` のリファクタ (cron から呼べる形に)
3. 古い記事 DELETE cron 新設 (`delete_old_unprocessed_extractions`)
   - 7 日経過した未処理 articles を DELETE (日次)
4. cron 起動の dispatch event 焼付
5. 救済対象判定 SQL (極めて単純 — `analyzable_articles` 存在 + `article_extractions` 不在
   + age 7 日以内)

成果: 失敗の自動救済 + 古い記事の自動 DELETE、運用不要。

**監視・アラート設計は本 PR scope 外** (§16 参照、監査基盤完成後の別 PR)。

## 10. 残論点

| # | 論点 | 状態 |
|---|---|---|
| §11 | content_hash の対象 (raw / sanitize 後 / TEMPLATE 全体) | ✓ post-truncate + post-sanitize で確定 |
| §12 | 失敗時 field 充填規律 (outcome_code 別の populate 表) | ✓ 確定 |
| §13 | Service Outcome variants と match pattern | ✓ 確定 |
| §14 | DELETE 実装の責務分離 (Service / task / repository どこで実行?) | ✓ 確定 |
| §15 | 古い記事の累積問題 | ✓ 7 日 cutoff + DELETE で確定 |
| §16 | 監視・アラート設計 | ✓ 監査基盤完成後に別 PR で検討 (本 PR scope 外) |

## 11. input_content の捕捉規律 — 確定

### 問題提起

`ExtractionPayload` には content に関する 3 field がある:

```python
input_content_head: str | None    # S: 先頭 2KB
input_content_length: int | None  # A': 全体長 (truncate 検知)
input_content_hash: str | None    # A: sha256 prefix 16 文字
```

content は extraction 経路で 4 段階の状態を経る。各 field がどの段階を指すか
明文化しないと audit の意味がブレる:

```
段階 1: article.original_content        (raw, DB 永続)
段階 2: content[:CONTENT_MAX_LENGTH]    (20,000 char 切詰、pre-sanitize)
段階 3: sanitize_for_untrusted_block(段階 2)  (post-sanitize、{content} 置換値)
段階 4: TEMPLATE.format(title=..., content=段階 3)  (最終 prompt 全体)
```

### 設計判断

| field | 対象段階 | 理由 |
|---|---|---|
| `input_content_length` | **段階 1 (raw)** | 「truncate されたか?」が判定できるのは原長との比較のみ。`length > CONTENT_MAX_LENGTH` で truncate 確定 |
| `input_content_head` | **段階 3 (post-sanitize、先頭 2KB)** | LLM が実際に見た先頭 2KB と一致 = forensic 価値最大 |
| `input_content_hash` | **段階 3 (post-sanitize、SHA-256 prefix 16)** | 「LLM が見た content が同一か?」の identifier。reproducibility と audit lineage の両立 |

### 段階 3 を hash 対象に選ぶ理由

- **TEMPLATE 部分は `prompt_version` で既に hash されている** ので段階 4 (rendered prompt 全体) を hash すると content と TEMPLATE が混ざり 2 軸が冗長
- **段階 1 (raw) を hash すると truncate 後の同一性を判定できない**
  - 例: 100KB 記事の先頭 20KB と、別の 50KB 記事の先頭 20KB が一致しても hash は別になる
  - LLM 入力としては同じものでも「content が同じか?」に答えられない
- **段階 2 (pre-sanitize) と段階 3 (post-sanitize) の差は sanitize 仕様の bug を audit で検出可能にする** ため後者を採用
  - sanitize 仕様が変わると同じ raw でも hash が変わる = 振る舞い差を捉えられる

### title の扱い — hash 対象には含めない

`{title}` も sanitize されて prompt に入るが、`input_content_hash` は **content 軸専用** で title は含めない。理由:

- title は短く (数十〜数百 char)、head + length が無くても直接 raw を payload に入れる方が実用的 (将来検討)
- field 名が `input_content_*` で content focus を明示している (title を混ぜると意味が曖昧)
- 同じ記事でも title が後から変わるケース (人間 edit) を audit したい時は別 field で扱うべき

### 実装の場所

`_call_api` / `extract` のどこで hash 計算するかは §13 (Service Outcome) で決める。
原則として **Service が article + Prompt を受け取った段階で計算**: raw を Service が
持っており、Prompt class の `render` を呼んだ後の値も知っているので、Service レイヤー
で 3 field を一括 populate するのが自然。

```python
# 概念コード
sanitized_content = sanitize_for_untrusted_block(
    article.original_content[:GeminiExtractionPrompt.CONTENT_MAX_LENGTH]
)
payload = ExtractionPayload(
    input_content_length=len(article.original_content),  # 段階 1
    input_content_head=sanitized_content[:2048],         # 段階 3 先頭
    input_content_hash=hashlib.sha256(
        sanitized_content.encode("utf-8")
    ).hexdigest()[:16],                                  # 段階 3 全体
    ...
)
```

なお `Prompt.render` は内部で sanitize + truncate するので、Service が
`render` を呼ぶだけだと sanitized_content を取り出せない。Prompt class に
**`prepare_content` のような中間 helper** を追加するか、Service が直接
`sanitize_for_untrusted_block` を呼ぶか、選択は §13 で決定。

## 12. 失敗時 field 充填規律 — 確定

### 設計判断 (再掲)

`reason_code` は payload に追加しない。既存 `outcome_code` (top-level column,
String(60), NOT NULL) で語彙を表現する (§2)。

### Field populate 表

`✓` = populate 必須、`–` = None で OK、`?` = 取れたら populate (best effort)

| outcome_code | source_name | ai_model | prompt_version | input_content_length | input_content_head | input_content_hash | ai_raw_response | entity_count | error_message | error_chain |
|---|---|---|---|---|---|---|---|---|---|---|
| `extracted` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | – | – |
| `skipped_already_extracted` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | – | – | – | – |
| `skipped_invalid_input` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | – | – | ✓ | ✓ |
| `ai_error_blocked_by_policy` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ? | – | ✓ | ✓ |
| `ai_error_input_too_large` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | – | – | ✓ | ✓ |
| `ai_error_provider` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ? | – | ✓ | ✓ |
| `ai_error_rate_limited` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | – | – | ✓ | ✓ |
| `ai_error_daily_quota_exhausted` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | – | – | ✓ | ✓ |
| `ai_error_network` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | – | – | ✓ | ✓ |
| `ai_error_config` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | – | – | ✓ | ✓ |
| `ai_error_insufficient_balance` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | – | – | ✓ | ✓ |
| `unclassified_error` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ? | – | ✓ | ✓ |
| `unexpected_error` | ✓ | ✓ | ✓ | ? | ? | ? | – | – | ✓ | ✓ |

### 規律の根拠

| 規律 | 理由 |
|---|---|
| `source_name` は常時 populate | A 級保険 — article DELETE で FK 切断後も追跡可能 |
| `ai_model` / `prompt_version` は常時 populate | Prompt class の `MODEL` / `VERSION` ClassVar から **状態に依存せず** 取れる |
| `input_content_*` は記事が存在する全 case で populate | article + sanitize で例外パスでも計算可能 |
| `ai_raw_response` は応答が返った時のみ | LLM call 完走前の例外 (rate limit / network) では存在しない |
| `entity_count` は parse 成功時のみ | parse 失敗で entity 数は意味を持たない |
| `error_*` は失敗時のみ | 成功 / skip パスには例外がない |

### 例外的扱い (`?` の解釈)

#### `ai_error_blocked_by_policy` の `ai_raw_response`

Gemini が SAFETY / RECITATION 等で応答を遮断した場合、**`response.text` が空または部分応答**。
SDK が部分テキストを返すなら populate (best effort)、`None` を返すなら `None`。
`ExtractionPolicyBlockedError(raw_response=...)` で **取れた分だけ保持** する設計。

#### `ai_error_provider` の `ai_raw_response`

`ProviderError` は 5xx (応答無し) と Pydantic ValidationError (応答あるが parse 失敗) の両方を含む。
parse 失敗のケースでは `response.text` が取れる → populate。5xx のケースでは応答無し → None。
**例外オブジェクトに `raw_response` を持たせる** ことで判別する (extractor 改修時に渡す)。

#### `unclassified_error` の `ai_raw_response`

`UnclassifiedError` は「provider が出すが既存階層に当てはまらない」例外。状況により応答が
取れる / 取れないが分かれる。best effort で populate。

#### `unexpected_error` の `input_content_*`

「予期しない例外」は **記事取得の前に死ぬ可能性** がある (例: Service injection 段階の AttributeError)。
記事 object が手元に無い場合は `None`。Service に到達後の例外なら populate。
**best effort** で扱い、必ず populate するとは保証しない。

#### `ai_error_input_too_large` の `input_content_length`

`length > 段階 2 の CONTENT_MAX_LENGTH` で truncate されている可能性が高いが、
truncate 後でも context window を超えた場合 (gemini-2.5-flash-lite の場合は
~1M tokens なので **実用上は到達しない**)。
length は段階 1 の値を populate するため、context window 超過の根拠データになる。

### 実装方針 — payload 組立 helper

成功 / skip / 失敗の 3 経路で **同じ field を同じ意味で populate** するため、共通 helper を用意:

```python
# app/analysis/extraction/audit.py (新規) — 概念コード
import hashlib
from typing import Any

from app.analysis.extraction.extractor.gemini_prompt import GeminiExtractionPrompt
from app.analysis.prompt_safety import sanitize_for_untrusted_block
from app.models.analyzable_article_record import AnalyzableArticleRecord


def base_extraction_payload_fields(article: AnalyzableArticleRecord) -> dict[str, Any]:
    """Service / tasks.py 両経路で共有する基底 field 群。

    記事と Prompt class から「状態に依存せず取れる」7 field を返す。
    成功時は entity_count / ai_raw_response を、失敗時は error_* を上乗せ。
    """
    raw = article.original_content
    sanitized = sanitize_for_untrusted_block(
        raw[: GeminiExtractionPrompt.CONTENT_MAX_LENGTH]
    )
    return {
        "source_name": article.source.name if article.source else None,
        "ai_model": GeminiExtractionPrompt.MODEL,
        "prompt_version": GeminiExtractionPrompt.VERSION,
        "input_content_length": len(raw),
        "input_content_head": sanitized[:2048],
        "input_content_hash": hashlib.sha256(
            sanitized.encode("utf-8")
        ).hexdigest()[:16],
    }
```

呼び出し側:
- **Service (成功)**: `ExtractionPayload(**base_extraction_payload_fields(article), entity_count=..., ai_raw_response=raw)`
- **Service (skip)**: `ExtractionPayload(**base_extraction_payload_fields(article))`
- **tasks.py (失敗)**: `_record_failure_event(payload_extra=base_extraction_payload_fields(article) | {"ai_raw_response": getattr(exc, "raw_response", None)})` の形で `build_failure_payload` に橋渡し
  - `error_message` / `error_chain` は `build_failure_payload` が exc から自動抽出する既存挙動を再利用

### この helper 配置の意図

- Service と tasks.py が **同じ field を同じ意味で populate する** ことを構造的に保証
- `GeminiExtractionPrompt.MODEL` / `VERSION` を **1 箇所で参照** することで provider 差し替え時の漏れを排除
- `sanitize_for_untrusted_block` の呼出を **payload 組立側に co-locate** して、render と audit で sanitize 結果が乖離しないことを保証 (両方 同じ式で計算)
- helper 配置場所は `app/analysis/extraction/audit.py` (Service / tasks 両方が import 可能なドメイン中立位置)

### 失敗時の `ai_raw_response` 配送経路

例外パスで raw を payload に詰めるには、**例外 object が `raw_response` を保持** する必要がある。
新設 2 例外と一部既存例外を改修して raw を保持させる:

```python
# 新設例外 (Stage 3 専用)
class ExtractionPolicyBlockedError(Exception):
    def __init__(self, *, finish_reason, raw_response: str | None = None,
                 prompt_version: str) -> None:
        self.finish_reason = finish_reason
        self.raw_response = (raw_response or "")[:2048] or None
        self.prompt_version = prompt_version
        super().__init__(f"blocked by policy: {finish_reason}")

class ExtractionInputTooLargeError(Exception):
    def __init__(self, *, prompt_version: str) -> None:
        self.prompt_version = prompt_version
        super().__init__("input too large for context window")
```

既存 `ProviderError` には `raw_response` 保持を **後付けで追加** (default None で
既存呼出箇所と互換)。tasks.py では `getattr(exc, "raw_response", None)` で
存在しない場合も安全に取れる形にする。

これで Service / tasks.py の **両経路で同じ規律が機械的に保証される**。

## 13. Service Outcome variants と match pattern — 確定

### 設計原則 (PR1/PR2 のパターン踏襲)

`IngestionService` / `ContentFetchService` の確立されたパターン:

- **既知の終端事象 (success / skip)** → Service が catch → Outcome として return + 同 tx audit 焼付
- **未知 / 失敗事象** → 例外として bubble up → tasks.py の except 節で audit 焼付 (別 tx)

Stage 3 もこのパターンに従う。

### Service Outcome 定義

`ExtractionService.extract(article)` の戻り値は `ExtractionOutcome` discriminated union:

```python
# app/analysis/extraction/application/outcomes.py (新規)
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExtractionPersisted:
    """成功 — article_extractions が永続化された。"""
    article_id: int
    entity_count: int


@dataclass(frozen=True, slots=True)
class ExtractionSkippedAlreadyDone:
    """冪等 skip — 既に extraction 済 (article_extractions row 存在)。"""
    article_id: int


@dataclass(frozen=True, slots=True)
class ExtractionSkippedInvalidInput:
    """provider が入力を拒否 (InvalidInputError catch)。retry なし、DELETE もしない。"""
    article_id: int
    reason: str  # InvalidInputError.message


ExtractionOutcome = (
    ExtractionPersisted
    | ExtractionSkippedAlreadyDone
    | ExtractionSkippedInvalidInput
)
```

### Service 内部の audit 焼付規律 (同 tx)

3 Outcome variant 全てで Service は **business state 更新と audit INSERT を同一 transaction** で commit:

| Outcome | event_type | outcome_code | business state |
|---|---|---|---|
| `ExtractionPersisted` | SUCCEEDED | `extracted` | article_extractions INSERT |
| `ExtractionSkippedAlreadyDone` | SKIPPED | `skipped_already_extracted` | (何もしない) |
| `ExtractionSkippedInvalidInput` | SKIPPED | `skipped_invalid_input` | (何もしない) |

PR1/PR2 の `_record_event` match pattern を踏襲。Service 内で:

```python
# app/analysis/extraction/application/extraction_service.py (改修概念)
async def extract(self, article: Article) -> ExtractionOutcome:
    async with self._session_factory() as session:
        # 冪等チェック
        if await self._already_extracted(session, article.id):
            await self._record_event(
                session,
                article=article,
                outcome=ExtractionSkippedAlreadyDone(article_id=article.id),
            )
            await session.commit()
            return ExtractionSkippedAlreadyDone(article_id=article.id)

        # AI 呼び出し (例外は外に bubble、tasks.py で受ける)
        try:
            call: ExtractionCall = await self._extractor.extract(
                title=article.title, content=article.original_content
            )
        except InvalidInputError as exc:
            # provider が入力を拒否 → SKIPPED outcome として閉じる
            outcome = ExtractionSkippedInvalidInput(
                article_id=article.id, reason=str(exc)
            )
            await self._record_event(session, article=article, outcome=outcome, exc=exc)
            await session.commit()
            return outcome

        # 永続化 + 成功 audit (同 tx)
        await self._persist_extraction(session, article, call)
        outcome = ExtractionPersisted(
            article_id=article.id, entity_count=len(call.result.entities)
        )
        await self._record_event(
            session, article=article, outcome=outcome, call=call
        )
        await session.commit()
        return outcome
```

`_record_event` 内で outcome variant で match:

```python
async def _record_event(
    self,
    session: AsyncSession,
    *,
    article: Article,
    outcome: ExtractionOutcome,
    call: ExtractionCall | None = None,
    exc: Exception | None = None,
) -> None:
    base = base_extraction_payload_fields(article)  # §12 helper
    match outcome:
        case ExtractionPersisted(entity_count=ec):
            payload = ExtractionPayload(
                **base,
                ai_raw_response=call.raw_response[:2048] if call else None,
                entity_count=ec,
            )
            event_type, outcome_code = EventType.SUCCEEDED, "extracted"
        case ExtractionSkippedAlreadyDone():
            payload = ExtractionPayload(**base)
            event_type, outcome_code = EventType.SKIPPED, "skipped_already_extracted"
        case ExtractionSkippedInvalidInput():
            payload = ExtractionPayload(
                **base,
                error_message=str(exc)[:2000] if exc else None,
                error_chain=[_fqn(exc)] if exc else None,
            )
            event_type, outcome_code = EventType.SKIPPED, "skipped_invalid_input"

    repo = PipelineEventRepository(session)
    await repo.append(
        stage=Stage.EXTRACTION,
        event_type=event_type,
        outcome_code=outcome_code,
        payload=payload,
        article_id=article.id,
        # ... attempt / duration_ms 等
    )
```

### tasks.py での match pattern

Service 戻り値を `match` 文で受け、例外は階層順に except する:

```python
# app/analysis/tasks.py (改修概念)
try:
    outcome = await service.extract(article)
    match outcome:
        case ExtractionPersisted(article_id=aid):
            await dispatch_to_classification(aid)  # Stage 4 へ
        case ExtractionSkippedAlreadyDone():
            return  # 既処理、何もしない
        case ExtractionSkippedInvalidInput():
            return  # provider が拒否、retry なし

# 内容起因 Permanent — articles DELETE
except ExtractionPolicyBlockedError as exc:
    await _record_failure_event(..., outcome_code="ai_error_blocked_by_policy", ...)
    await _delete_unprocessable_article(article.id)  # §14 参照
except ExtractionInputTooLargeError as exc:
    await _record_failure_event(..., outcome_code="ai_error_input_too_large", ...)
    await _delete_unprocessable_article(article.id)

# 環境起因 Permanent — articles 残す
except ConfigurationError as exc:
    await _record_failure_event(..., outcome_code="ai_error_config", ...)
except InsufficientBalanceError as exc:
    await _record_failure_event(..., outcome_code="ai_error_insufficient_balance", ...)

# Transient (cron 救済対象)
except DailyQuotaExhaustedError as exc:
    await _record_failure_event(..., outcome_code="ai_error_daily_quota_exhausted", ...)
except RateLimitError as exc:
    await _record_failure_event(..., outcome_code="ai_error_rate_limited", ...)
except NetworkError as exc:
    await _record_failure_event(..., outcome_code="ai_error_network", ...)
except ProviderError as exc:
    await _record_failure_event(..., outcome_code="ai_error_provider", ...)
except UnclassifiedError as exc:
    await _record_failure_event(..., outcome_code="unclassified_error", ...)
except Exception as exc:
    await _record_failure_event(..., outcome_code="unexpected_error", ...)
```

### Outcome 設計のポイント

#### 1. `frozen=True, slots=True` で immutable

PR1/PR2 と同様。Outcome は値オブジェクト、変更不可。

#### 2. 失敗を Outcome に含めない (例外で bubble)

成功 / skip は Service が責任もって audit 焼付して返すが、失敗は **tasks.py に委譲**。
理由:
- 失敗時は Service の tx が rollback される (DB 状態が一貫しない)
- audit は **別 session で別 tx** が必要 (§4 参照)
- DELETE 等の後続処理判断は tasks.py の責務 (Service は記事を消すべきか知らない)

#### 3. `ExtractionPersisted` には `entity_count` を含む

Stage 4 (classification) dispatch 判断に使えるかもしれないし、tasks.py のロギングにも使える。
PR1/PR2 で `IngestionOutcome` が件数を含むのと同様。

#### 4. `ExtractionSkippedInvalidInput` を Outcome に含める理由

`InvalidInputError` は **provider が入力を拒否** する明示的な skip 事象。これは:
- 失敗 (retry すべき) ではない
- 既知の終端 (provider の判断が同じ入力では変わらない可能性が高い)
- → Outcome として閉じるのが自然 (例外 bubble させるなら DELETE すべきか? という別判断が要る)

これを skip として閉じることで、tasks.py 側の except 節がシンプルになる。

#### 5. `payload_extra` 経由でなく Outcome で扱う

`InvalidInputError` を Service が catch して outcome に翻訳することで、
`base_extraction_payload_fields` を Service 内で 1 回呼べば済む (tasks.py との二重実行回避)。

## 14. DELETE 実装の責務分離 — 確定

### 設計判断

**tasks.py が DELETE 用の Service method を呼ぶ**。Service が DELETE + audit を **同一 transaction** で実行。

```
tasks.py except 節
  ↓ (例外 catch + DELETE 判定)
ExtractionService.mark_article_unprocessable(article_id, *, outcome_code, exc)
  ↓ (1 tx 内で実行)
  1. INSERT pipeline_events (article_id=X, outcome_code=...)
  2. DELETE FROM analyzable_articles WHERE id=X
  3. commit
  ↓ (commit 完了)
  → CASCADE で article_extractions / extraction_noises / article_analysis 削除
  → ondelete=SET NULL で pipeline_events.article_id が NULL に更新
```

### 各層の責務

| 層 | 責務 |
|---|---|
| **tasks.py** | 例外 catch → 「DELETE すべきか」の判定 (内容起因 Permanent のみ) → Service method を呼ぶ |
| **ExtractionService** | DELETE + audit を同一 tx で実行 (`mark_article_unprocessable` method 新設) |
| **ArticleRepository** | DELETE の実 SQL を実行 (Service が呼ぶ) |
| **PipelineEventRepository** | audit INSERT (既存、Service が呼ぶ) |

### 命名と配置

```python
# app/analysis/extraction/application/extraction_service.py (新 method 追加)
async def mark_article_unprocessable(
    self,
    article_id: int,
    *,
    outcome_code: str,  # "ai_error_blocked_by_policy" or "ai_error_input_too_large"
    exc: BaseException,
) -> None:
    """内容起因 Permanent failure: article DELETE + audit を同 tx で実行。

    呼出は tasks.py の except 節 (ExtractionPolicyBlockedError /
    ExtractionInputTooLargeError) 限定。他の経路から呼ばないこと。
    """
    async with self._session_factory() as session:
        # 1. audit event INSERT (article_id=X 記録、source_name は payload に保存)
        article = await self._article_repo(session).get(article_id)
        if article is None:
            # 既に消えている (race) — audit だけ残す
            ...
            return
        payload = build_failure_payload(
            Stage.EXTRACTION, exc,
            extra=base_extraction_payload_fields(article) | {
                "ai_raw_response": getattr(exc, "raw_response", None),
            },
        )
        repo = PipelineEventRepository(session)
        await repo.append(
            stage=Stage.EXTRACTION,
            event_type=EventType.FAILED,
            outcome_code=outcome_code,
            payload=payload,
            article_id=article.id,
            error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
        )

        # 2. article DELETE (CASCADE で関連 row、SET NULL で audit.article_id)
        await self._article_repo(session).delete(article.id)

        # 3. commit
        await session.commit()
```

### tx 内の順序 — audit INSERT が先、DELETE が後

理由:
1. **同 tx だが、INSERT の execute 順を後にすると `ondelete=SET NULL` が即座に作用** して
   article_id=X の audit が article_id=NULL になってしまう (commit 前でも、SQL 実行順で値が変わる)
2. **先に audit を書いておけば**、その後 DELETE の CASCADE / SET NULL が走り、結果として
   audit row は article_id=NULL に更新される ← 期待通り
3. もし DELETE → INSERT の順だと、INSERT 時点で article は既に存在しない。
   PipelineEventRepository が `source_id` を逆引きする際に SELECT で article が見つからず
   `source_id=NULL` になる (これは A 級保険として OK だが、source_id を保持できる方が望ましい)

→ INSERT 先行が **A 級保険を最大化** する。

### tasks.py での呼び出し例

```python
# app/analysis/tasks.py (DELETE 経路の改修概念)
except ExtractionPolicyBlockedError as exc:
    await service.mark_article_unprocessable(
        article.id,
        outcome_code="ai_error_blocked_by_policy",
        exc=exc,
    )
except ExtractionInputTooLargeError as exc:
    await service.mark_article_unprocessable(
        article.id,
        outcome_code="ai_error_input_too_large",
        exc=exc,
    )
```

別途 `_record_failure_event` を呼ぶ必要は無い (Service 内で audit 書込済)。

### この責務分離の意図

#### 1. atomicity 保証

DELETE と audit が同 tx → **「audit 書いた後で DELETE 失敗」「DELETE した後で audit 失敗」がどちらも起きない**。
別 tx だと audit と DELETE の整合性が壊れる可能性がある (audit はあるが article は残る、または逆)。

#### 2. tasks.py の責務を「dispatch 判断」に閉じ込める

tasks.py は「どの outcome_code か」と「DELETE するか / しないか」だけを判断する。実際の SQL 操作は Service に委譲。
これは PR1/PR2 (`IngestionService` / `ContentFetchService`) と同じ責務分離。

#### 3. DELETE 経路を 1 method に閉じる

`mark_article_unprocessable` 以外で article DELETE を呼ばない (CLI スクリプト等は除く)。
これにより「DELETE 経路は audit を伴う」が Service の API 契約として保証される。

#### 4. 通常の失敗パス (transient / env) との明確な区別

通常の失敗パス (cron 救済対象 / 環境起因) は tasks.py の `_record_failure_event` で別 tx audit のみ。
DELETE 経路だけが Service method 経由 → DELETE が走るタイミングが構造的に明確。

### race condition 考慮

複数の worker が同じ article を処理している場合:
- worker A が DELETE する瞬間に worker B が同じ article を SELECT 中
- worker B の操作は外部キー制約で fail or 古い参照のまま

これは **稀** (Stage 3 は冪等チェックで二重処理を弾く) だが、
`mark_article_unprocessable` 内で `article is None` チェックを入れて防御:

```python
article = await self._article_repo(session).get(article_id)
if article is None:
    # 既に削除済 (race or 重複呼出) — audit だけ残して return
    ...
```

これで race-loss 時も audit は残る (article_id=NULL の event)。

## 15. 古い記事の累積問題 — 確定

### 問題提起

「処理可能」と判定されて articles に残った記事のうち、何度 cron が試行しても
成功しない記事が累積する可能性。例えば連日 quota 枯渇、隠れた永続的問題等。

### 設計判断 (2026-05-07 確定)

**ニュースは新鮮さが本質的価値**なので、age cutoff (時間軸) で「分析価値なし」
を判定する。回数判断と違い、時間軸はニュース自身の老化という外部的・本質的な
軸 (LLM 能力に依存しない) なので、ユーザー哲学「回数で判断しない」に反しない。

| 軸 | 性質 | 採用 |
|---|---|---|
| 回数 | LLM 能力依存、恣意的 | ✗ 採用しない |
| 時間 | ニュース固有の老化、本質的 | ✓ 採用 |

### 確定事項

- **age cutoff: 7 日** (Vector はテックニュース中心、1 週間で実質価値消失)
- **救済 cron: 新鮮な記事 (7 日以内) のみ対象**
- **DELETE cron (日次): 7 日経過した未処理記事を削除**
  - 状態空間が「存在する (救済対象)」/「存在しない (諦めた)」の二値に統一
  - 「処理不能 (内容起因)」と「古すぎ (時間起因)」が同じ DELETE 操作
  - audit は pipeline_events に `article_id=NULL` で残る

### SQL イメージ

```sql
-- 救済 cron 対象 (新鮮な記事のみ)
SELECT a.id FROM analyzable_articles a
LEFT JOIN article_extractions ae ON a.id = ae.article_id
WHERE ae.id IS NULL
  AND a.created_at > NOW() - INTERVAL '7 days';

-- DELETE cron (日次、古い未処理を削除)
DELETE FROM analyzable_articles
WHERE id IN (
  SELECT a.id FROM analyzable_articles a
  LEFT JOIN article_extractions ae ON a.id = ae.article_id
  WHERE ae.id IS NULL AND a.created_at < NOW() - INTERVAL '7 days'
);
```

## 16. 監視・アラート設計 — Logfire 統合は別 spec へ

### 方針 (2026-05-07 確定)

監査基盤と Logfire は **競合せず補完する層**。役割分担:

- **監査 (pipeline_events)**: ビジネスイベントの永続記録、レトロスペクティブ
  SQL 分析 (source 別失敗率 / prompt_version 別 / 等)
- **Logfire**: オペレーショナル可観測性、リアルタイム挙動 (P99 レイテンシ /
  worker エラー率 / span trace)
- エラー / 例外は両方に出す (粒度が違う)

Logfire の進行ロードマップは独立した spec へ:
**`specs/logfire-integration.md` 参照**

### 本 PR (PR3-a) との関係

- PR3-a 系統と Logfire 統合 (PR-L1) は責務が独立、**並行可能**
- 監視・アラートのカスタム計装 / しきい値設定は **本番観察 1-2 週間後**
  に Phase 3 で着手 (本 PR scope 外)
- 古い未処理記事の累積監視 (§15 由来) は Logfire spec の Phase 3 残論点に移管
