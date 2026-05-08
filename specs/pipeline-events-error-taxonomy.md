# pipeline_events エラー分類学 (Error Taxonomy) 設計

PR3-a-1 (Stage 3 extraction 監査統合) で得た知見をもとに、pipeline_events
書込側の **エラー型階層 / outcome_code 設計** を再整理する設計メモ。

ADR: `docs/observability/pipeline-events-design.md`
関連: `specs/pipeline-events-stage2-design.md`
ロードマップ: memory `project_pipeline_events_pr_roadmap.md`

履歴:
- 2026-05-08 初版 (Layer 1 / Layer 2 分離 + outcome_code を type.CODE 投影に格下げ)
- 2026-05-08 改訂 (AI provider 由来エラー 10 種を確定 + `AIProvider*Error` 命名規約 + 配置 `app/analysis/errors/provider.py`)
- 2026-05-08 再改訂 (format 違反は工程エラー扱い: Layer 2-A から ResponseInvalid を削除し各 Stage の Layer 2-B に分散、UnknownCategorySlug を Retryable 化、`NonRetryableDropArticle` は provider 明示拒否 2 種に厳密化、retry 上限到達分は cron TTL 救済モデルへ)
- 2026-05-08 三改訂 (`unknown` は型階層から外し、DB `category` 値 + catch-all ラベルとしてのみ存在させる。Layer 1 dispatch marker は 5 種 (Exception 3 + Outcome 2)、DB CHECK 値は 6 値 (5 + `unknown`))
- 2026-05-08 四改訂 (Outcome は成功のみ、失敗は typed exception で raise。`InvalidInputError` / `InvalidInputOutcome` 廃止し `AIProviderInputRejectedError` (DROP) と `ExtractionResponseInvalidError` (RETRYABLE) に分離。Service が翻訳責務、Task が Layer 1 dispatch 責務。payload 標準 field set を確定)

---

## 背景

PR3-a-1 を実装する過程で、Task 層に **10 except 節が縦に並ぶ** 状態が出現した。

```python
except ExtractionPolicyBlockedError as exc:
    await svc.mark_article_unprocessable(..., outcome_code="ai_error_blocked_by_policy", ...)
    return
except ExtractionInputTooLargeError as exc:
    await svc.mark_article_unprocessable(..., outcome_code="ai_error_input_too_large", ...)
    return
except ConfigurationError as exc:
    await _audit_extraction_failure(..., outcome_code="ai_error_config", ...)
    return
# ... 続く 7 節
```

これは以下 3 つの構造問題の症状:

1. **`AnalysisDomainError` 階層が dispatch 軸を表現していない** — 8 個の例外型が「原因
   別」だけで分類され、Task 層が「Skip / Permanent / Transient / 内容起因 vs 環境起因」
   を判別できない
2. **outcome_code が独立した parallel registry になっていた** — ADR §12 に列挙された
   13 個の語彙が型階層と紐付いておらず、call site で literal hard-code される
3. **AnalysisDomainError という名前自体が嘘** — 中身は全部 AI 呼び出しインフラ起因。
   stage 固有のドメインエラー (例: `unknown category slug`) を `ProviderError` で
   流用する事故が `classification/service.py:138` で実際に起きている

---

## 設計原則

### 原則 1: 例外階層は 2 軸で構成する

例外型は **dispatch 軸 (Layer 1)** と **origin 軸 (Layer 2)** の **多重継承** で表現する。

```
Layer 1 dispatch marker (型階層 = 5 種): Task 層がここで `isinstance` 分岐
  ├ SuccessOutcome              (成功 — Outcome dataclass)
  ├ IdempotentSkipOutcome       (冪等スキップ — Outcome dataclass)
  ├ RetryableError              (例外、cron 救済対象)
  ├ NonRetryableDropArticle     (例外、内容起因 permanent — 記事 DELETE)
  └ NonRetryableKeepArticle     (例外、環境起因 permanent — 記事保持、運用者対応)

  ※ DB `category` カラム値としては上記 5 + `unknown` の 6 値。`unknown` は
    catch-all (`except Exception`) の監査ラベルであって、型階層には登場しない
    (`UnknownFailure` 等の型は作らない — §設計判断 参照)

Layer 2-A (AI 呼び出し起因、stage 横断で共有 / 9 種):
  AIProviderError
    ├ AIProviderConfigurationError(NonRetryableKeepArticle)
    ├ AIProviderRequestInvalidError(NonRetryableKeepArticle)
    ├ AIProviderInsufficientBalanceError(NonRetryableKeepArticle)
    ├ AIProviderRateLimitedError(RetryableError)              # INLINE_RETRY=False
    ├ AIProviderQuotaExhaustedError(RetryableError)           # INLINE_RETRY=False
    ├ AIProviderServiceUnavailableError(RetryableError)       # INLINE_RETRY=True
    ├ AIProviderNetworkError(RetryableError)                  # INLINE_RETRY=True
    ├ AIProviderInputRejectedError(NonRetryableDropArticle)   # token超過/入力safety/拒否
    └ AIProviderOutputBlockedError(NonRetryableDropArticle)   # 出力safety/recitation

Layer 2-B (stage 固有ドメインエラー、stage ごと):
  ExtractionDomainError
    └ ExtractionResponseInvalidError(RetryableError)          # INLINE_RETRY=True
  ClassificationDomainError
    ├ ClassificationResponseInvalidError(RetryableError)      # INLINE_RETRY=True
    └ UnknownCategorySlugError(RetryableError)                # INLINE_RETRY=True
  EmbeddingDomainError
    └ EmbeddingResponseInvalidError(RetryableError)           # INLINE_RETRY=True
```

**重要な配分原則** (2026-05-08 再改訂):

- **`NonRetryableDropArticle` (即削除)** は **provider が明示的に処理不可と返したケース 2 種のみ**:
  `AIProviderInputRejectedError` / `AIProviderOutputBlockedError`
- **format 違反系 (parse 不能、schema 違反、unknown slug 等) は `RetryableError`** に分類:
  AI モデルの揺らぎで retry 救済が現実的に効くため。retry 上限到達分は記事保持
  + cron TTL で掃除
- **「使える応答か」の判定基準は stage ごとに違う** ため、format 違反系は Layer 2-A
  で一括せず各 Stage の Layer 2-B (`<Stage>ResponseInvalidError`) に分散

各具体型は **多重継承で Layer 1 と紐付き**、`CODE: ClassVar[str]` を pin する。

### 原則 2: outcome_code は独立概念ではなく Layer 2 type.CODE の投影

旧案では outcome_code を ADR §12 に parallel registry として列挙していたが、これは
**「2 つの真実」(型階層 / ADR) のズレ事故** を構造的に誘発する。

新案では outcome_code は **Python 型に CODE を pin した結果の投影** として位置づける:

```python
class AIProviderRateLimitedError(AIProviderError, RetryableError):
    CODE: ClassVar[str] = "ai_error_rate_limited"

class UnknownCategorySlug(ClassificationDomainError, NonRetryableDropArticle):
    CODE: ClassVar[str] = "unknown_category_slug"

@dataclass(frozen=True, slots=True)
class ExtractedOutcome(SuccessOutcome):
    CODE: ClassVar[str] = "extracted"
    extraction: Extraction

@dataclass(frozen=True, slots=True)
class AlreadyExtractedOutcome(IdempotentSkipOutcome):
    CODE: ClassVar[str] = "already_extracted"
```

ADR §12 の outcome_code 一覧は **「型から自動導出される一覧表」** に格下げし、
Python 型階層を SSoT にする。

### 原則 3: 監査行は型から projection で生成する

Service / Task は **マッピング辞書も if/elif の山も書かない**:

```python
def _category_of(obj: object) -> Layer1Category:
    """Layer 2 オブジェクトから Layer 1 大枠を取り出す。"""
    match obj:
        case SuccessOutcome():               return "success"
        case IdempotentSkipOutcome():        return "idempotent_skip"
        case RetryableError():               return "retryable"
        case NonRetryableDropArticle():      return "non_retryable_drop_article"
        case NonRetryableKeepArticle():      return "non_retryable_keep_article"
        case _:                              return "unknown"

# 失敗経路
except (NonRetryableDropArticle, NonRetryableKeepArticle, RetryableError) as exc:
    await event_repo.append(
        stage=Stage.EXTRACTION,
        category=_category_of(exc),                  # ← Layer 1
        code=type(exc).CODE,                         # ← Layer 2 (type.CODE)
        error_class=type(exc).__qualname__,          # ← FQN (forensics)
        payload=ExtractionPayload(...),              # ← 詳細
    )
```

`isinstance` / `match` 1 つで Layer 1 が取れる。マッピングは型システム経由で宣言される。

### 原則 4: Outcome は成功のみ、失敗は typed exception で raise

Service の return 型 (`<Stage>Outcome` union) には **成功種別だけを入れる**。失敗は
全て typed exception (Layer 2-A or 2-B) として raise する。

```python
# 成功種別のみ — 「次にどう進むか」が分岐軸
ExtractionOutcome = ExtractedOutcome | NoiseOutcome

# 失敗は全て raise (Outcome union に入れない)
class AIProviderInputRejectedError(AIProviderError, NonRetryableDropArticle): ...
class ExtractionResponseInvalidError(ExtractionDomainError, RetryableError): ...
```

**なぜ Outcome に失敗を混ぜないか**:

- 「成功で止まる」(`NoiseOutcome` = noise として正常完了) と「失敗で止まる」は本質
  が違う。前者は通常運用 (別テーブル永続化済)、後者は異常系 (audit / 削除 / alert
  / retry が必要)
- Outcome union に失敗を入れると Task 層が `isinstance` で「成功 or 失敗」を判定
  しなければならず、Layer 1 dispatch の意味がなくなる
- Outcome の名前で失敗種別を決め打ちすると別の失敗型を別 Outcome にしないといけ
  なくなり、Outcome union が失敗型ごとに膨張する → Layer 1 marker と二重管理
- retry すべき失敗を Outcome として return すると Task は「完了」と扱うので retry
  が走らない、これも構造的な誤り

**責務分担**:

| 層 | 責務 | 何を扱うか |
|---|---|---|
| AI Client (extractor) | provider と話す、provider 例外を Layer 2-A に翻訳 | provider SDK 例外 → `AIProvider*Error` |
| Service | 業務制約検証、format 違反を Layer 2-B に翻訳 | Pydantic ValidationError、業務 invariant 違反 → `<Stage>*Error` |
| Task | Layer 1 marker で dispatch (大枠だけ判断) | `RetryableError` / `NonRetryableDropArticle` / `NonRetryableKeepArticle` / catch-all |

Service は「**失敗の翻訳**」、Task は「**失敗の orchestration**」。前者が概念化、
後者が処理。境界が綺麗に分かれる。

---

## Layer 1 大枠の適用範囲 — article-bound analysis stages 専用

`Layer1Category` 6 値は **article-bound analysis stages** (extraction /
classification / embedding) の処理結果分類である。これらの stage では article 1 件
に対する処理結果が明確に 6 値のいずれかに分類できる。

一方、article 化前の stages (dispatch / source_fetch / content_fetch) では Layer 1
6 値 (とくに `non_retryable_drop_article` / `non_retryable_keep_article`) の語彙が
合わない。これらの stage は `pipeline_events.category` を **NULL** のまま記録する
(意味のある分類は `event_type` + `outcome_code` で表現済み)。

**DB レベルでは `pipeline_events.category` は nullable** で、CHECK 制約は
`category IS NULL OR category IN (6 values)` の形を取る。NULL は「未分類
(article-bound analysis 以外の stage)」として正当な値である。

PR3.5-b で nullable 列として追加。PR3.5-c で analysis 系 raise/catch を新型例外に
切替えると、analysis 系 call site から category 値が入り始める。collection 系
(dispatch / source_fetch / content_fetch) は引き続き NULL のまま記録される。

> 旧版で「PR3.5 末で `category` を NOT NULL 化」と書いていた Phase C 計画は撤回
> (analysis 専用に位置付けたため、全行 NOT NULL 化は不可)。集計が必要なら
> `WHERE category IS NOT NULL` partial index を後追いする運用方針。

---

## Layer 1 大枠 — 型 5 種 / DB 値 6 種

### 一覧 (Python 型階層 = 5 種)

| # | category | 例外/Outcome 基底 | event_type | Task 層動作 | 後続 |
|---|---|---|---|---|---|
| 1 | `success` | `SuccessOutcome` (Outcome dataclass 基底) | SUCCEEDED | 永続化 + 下流 chain | 通常 |
| 2 | `idempotent_skip` | `IdempotentSkipOutcome` (Outcome dataclass 基底) | SUCCEEDED | 何もしない | なし |
| 3 | `retryable` | `RetryableError` (Exception 基底) | FAILED | inline retry 余地あれば raise、なければ audit + return | cron 救済 |
| 4 | `non_retryable_drop_article` | `NonRetryableDropArticle` (Exception 基底) | FAILED | `mark_article_unprocessable` + audit + return | なし (記事削除) |
| 5 | `non_retryable_keep_article` | `NonRetryableKeepArticle` (Exception 基底) | FAILED | audit + return + 運用者 alert | 修復後 cron で復旧 |

### catch-all `unknown` (型階層の外)

| # | category | 経路 | event_type | Task 層動作 |
|---|---|---|---|---|
| 6 | `unknown` | `except Exception as exc:` (上記 5 種いずれにも `isinstance` マッチしなかった残余) | FAILED | audit + return (記事保持、cron TTL 救済対象) |

`unknown` は **DB レベルの category 値 / 監査ラベル** としてのみ存在する。Python 型
階層には対応する基底クラスを置かない (`UnknownFailure` 等は作らない — §設計判断
の根拠 参照)。

### 4 種 vs 5 種の判断根拠

ユーザー初期提案は 4 種 (`success` / `idempotent_skip` / `retryable` /
`non_retryable_delete_article`)。これに「環境起因 permanent (記事は健全、人間が直す)」が
欠ける:

| 例外 | 性質 | 4 種だと | 5 種なら |
|---|---|---|---|
| `AIConfigurationError` | リトライしても直らない、記事は健全 | 入れる場所がない | `NonRetryableKeepArticle` |
| `AIInsufficientBalance` (DeepSeek 402) | リトライしても直らない、記事は健全 | 入れる場所がない | `NonRetryableKeepArticle` |

「リトライ不可能」を 1 つにまとめて記事削除を強制すると、**API key を直し忘れただけで
記事が大量に消える** 事故が起きうる。記事の運命を category 単位で分けるのが安全。

### inline retry の扱い

`RetryableError` は cron 救済対象だが、その内 `AINetworkError` / `AIProviderUnavailable`
等は **taskiq の即時 retry が有効な可能性がある** (数秒の瞬断)。これらは Layer 2 で
`INLINE_RETRY: ClassVar[bool] = True` を pin し、Task 層が:

```python
except RetryableError as exc:
    if type(exc).INLINE_RETRY and not is_last_attempt(ctx):
        raise  # taskiq が retry
    await _audit_failure(..., category="retryable", code=type(exc).CODE, ...)
    return
```

で扱う。Layer 1 を増やさず、Layer 2 の属性で表現する (Layer 1 は dispatch 軸の一貫性
を保つ)。

---

## DB schema

### 新 column 構成

| column | 型 | NULL | 由来 | 例 |
|---|---|---|---|---|
| `category` | TEXT | **NULL** (article-bound analysis stages のみ値、他は NULL) | Layer 1 大枠 (`isinstance` で取得) | `retryable` |
| `code` | TEXT | **NULL** (article-bound analysis stages のみ値) | Layer 2 `type.CODE` | `ai_error_rate_limited` |
| `error_class` | TEXT | NULL | failure 時のみ `type(exc).__qualname__` | `AIRateLimited` |
| `payload` | JSONB | NOT NULL | 詳細 (生 message / chain / raw response) | `{...}` |

### category と code を両方持つ理由

| query | category だけ | code だけ | 両方 |
|---|---|---|---|
| 直近 24h、retry 対象数 | 一発 | code→category マップ SQL 必須 | 一発 |
| `ai_error_rate_limited` の頻度推移 | 出ない (粗い) | 一発 | 一発 |
| DELETE article 系合計 | `category='non_retryable_drop_article'` | 該当 code を OR で列挙 | 一発 |
| 大枠別カウント + drill-down | drill 不可 | CASE で再構成 | 自然 |

→ **両方 column 化が SQL / dashboard / runbook で素直**。

### CHECK 制約

```sql
ALTER TABLE pipeline_events ADD CONSTRAINT ck_category CHECK (
  category IS NULL                              -- article 化前の stages (dispatch / source_fetch / content_fetch)
  OR category IN (
    'success',
    'idempotent_skip',
    'retryable',
    'non_retryable_drop_article',
    'non_retryable_keep_article',
    'unknown'                                  -- catch-all (PR3.6 の Exception 経路)
  )
);
```

`code` には CHECK を付けない (型に CODE pin したものが SSoT、ADR は導出資料、
動的に増減する)。

### 旧 outcome_code との互換期間

PR3-a-1 merge 時点では `outcome_code` のみ存在。新 column 移行は段階的:

1. **Phase A** (PR3.5): `category` / `code` / `error_class` 列を追加 (NOT NULL は外す)。
   既存 `outcome_code` は維持、新書込で `code` に同値を入れる
2. **Phase B** (PR3.6): Task 層を 3 except + catch-all に集約、`type(exc).CODE` 経由で
   `code` を埋める
3. **Phase C** (PR3.7): 全ての書込が新 column を埋めていることを確認後、`category` /
   `code` を NOT NULL 化、`outcome_code` を `code` の generated column に格下げ
4. **Phase D** (PR3.8 任意): 互換期間終了後に `outcome_code` を DROP

### payload の standard field set (失敗時)

失敗時に `payload` JSONB に焼く field を標準化する。Stage 共通の必須 field と
Stage 固有 field に分け、Pydantic discriminated union (`ExtractionPayload` 等) で
型保証する。

| field | 由来 | 共通 / Stage 固有 | 例 |
|---|---|---|---|
| `message` | `str(exc)` | 共通 | `"AI returned schema-violating response"` |
| `error_class` | `type(exc).__qualname__` (column 重複だが grep 用に payload にも保持可) | 共通 | `"ExtractionResponseInvalidError"` |
| `attempt` | taskiq retry_count + 1 | 共通 | `2` |
| `validation_errors` | Pydantic ValidationError の `.errors()` 等 | 共通 (該当時のみ) | `[{"loc": ["entities", 0, "name"], "msg": "..."}]` |
| `raw_response` | AI の原応答 (parse 失敗時の forensics) | 共通 (該当時のみ、文字数制限 4KB 推奨) | `"```json\n{...broken...}"` |
| `prompt_version` | prompt のバージョン識別子 | 共通 | `"extraction.v3.2"` |
| `article_id` | ready.article_id | Stage 3 / 4 固有 | `12345` |
| `extraction_id` | ready.extraction_id | Stage 4 / 5 固有 | `67890` |
| `model` | extractor / classifier / embedder の MODEL | 共通 | `"gemini-2.0-flash"` |
| `chain` | 例外 chain (再帰的に `__cause__`) | 共通 (該当時のみ) | `[{"class": "ValidationError", "message": "..."}]` |

**設計方針**:

- payload の **schema 自体は Stage ごとに違う** (`ExtractionPayload` /
  `ClassificationPayload` / `EmbeddingPayload`) が、上記 field の命名と意味は揃える
- `validation_errors` / `raw_response` / `chain` は **発生したときだけ** 入れる
  (None でも空 list でもなく field 自体を omit)
- payload を読むのは人間 (運用調査) と dashboard クエリ。`validation_errors` を
  GIN index で検索可能にする予定 (PR3.7 以降)

### event_type の冗長性

新 schema では `event_type` が `category` から導出可能になる:

| category | event_type |
|---|---|
| success | SUCCEEDED |
| idempotent_skip | SUCCEEDED (or SKIPPED — 議論余地) |
| retryable / non_retryable_* | FAILED |

`event_type` を残すか廃止するかは別論点として PR3.8 に持ち越し。当面は両方持つ。

---

## Layer 2 型定義 (各 stage 共通基盤)

### 共通 dispatch marker

```python
# app/observability/categories.py (Layer 1 marker / category 値の SSoT)

class Layer1Category(StrEnum):
    """DB `pipeline_events.category` カラムに書き込む値の集合。

    `UNKNOWN` は catch-all (`except Exception`) 経路で直接代入される
    監査ラベルであり、Python 型階層には対応するクラスが存在しない。
    """
    SUCCESS = "success"
    IDEMPOTENT_SKIP = "idempotent_skip"
    RETRYABLE = "retryable"
    NON_RETRYABLE_DROP_ARTICLE = "non_retryable_drop_article"
    NON_RETRYABLE_KEEP_ARTICLE = "non_retryable_keep_article"
    UNKNOWN = "unknown"  # 型階層には無い、catch-all 専用ラベル


# Layer 1 例外基底 (空クラス、dispatch marker — 5 種のうちの 3 種)
class RetryableError(Exception): ...
class NonRetryableDropArticle(Exception): ...
class NonRetryableKeepArticle(Exception): ...


# Layer 1 Outcome 基底 (空 dataclass、dispatch marker — 5 種のうちの 2 種)
class SuccessOutcome: ...           # subclass が dataclass 化
class IdempotentSkipOutcome: ...

# 注: `UnknownFailure` のような型は **作らない**。理由は §設計判断 参照。
```

### AI 呼び出しエラー (stage 横断、Layer 2-A / 9 種)

#### 概念定義 — 「外部モデル API 呼び出し」に内在的に起きうる失敗

| 概念 | 意味 | Layer 1 |
|---|---|---|
| `AIProviderConfigurationError` | API key、model 名、endpoint、provider option など呼び出し前提の設定が不正 | NonRetryableKeepArticle |
| `AIProviderRequestInvalidError` | provider API 仕様に対する request 構築が不正 (必須 field 抜け、tool schema 違反など) | NonRetryableKeepArticle |
| `AIProviderInsufficientBalanceError` | 残高不足、契約プラン上限、支払い方法の問題 | NonRetryableKeepArticle |
| `AIProviderRateLimitedError` | 短期レート超過 (RPM、並行リクエスト数)。秒〜分で解消 | RetryableError (wait) |
| `AIProviderQuotaExhaustedError` | 長期 quota 到達 (RPD、月次)。時間〜日で解消 | RetryableError (wait) |
| `AIProviderServiceUnavailableError` | provider 5xx、一時障害 | RetryableError (inline) |
| `AIProviderNetworkError` | timeout、DNS 失敗、接続拒否 | RetryableError (inline) |
| `AIProviderInputRejectedError` | token 超過、入力 safety、provider が入力段階で拒否 (永久不可と確信できるケース) | **NonRetryableDropArticle** |
| `AIProviderOutputBlockedError` | safety / recitation 等で出力が遮断 (finish_reason、永久不可と確信できるケース) | **NonRetryableDropArticle** |

> **format 違反 (parse 不能、空応答、schema 違反、truncated 等) は Layer 2-A から
> 除外** し、各 Stage の Layer 2-B (`<Stage>ResponseInvalidError`) に分散した。
> 「使える応答か」の基準は Stage ごとに違うため。詳細は §設計判断の根拠 参照。

#### 命名規約

- **基底**: `AIProviderError` (Exception 直下)
- **子クラス**: `AIProvider<Concept>Error` の形式で全て `AIProvider` プレフィックス + `Error` サフィックス を付与
- **CODE prefix**: `ai_error_` (Python 名から派生する snake_case)
- **多重継承**: Layer 2 (`AIProviderError`) + Layer 1 marker (`RetryableError` / `NonRetryableDropArticle` / `NonRetryableKeepArticle`)
- **配置**: `app/analysis/errors/provider.py` (将来 `extraction.py` `classification.py` `embedding.py` を兄弟に置けるよう ディレクトリ化)

#### 型定義

```python
# app/analysis/errors/provider.py

from typing import ClassVar

from app.observability.categories import (
    NonRetryableDropArticle,
    NonRetryableKeepArticle,
    RetryableError,
)


class AIProviderError(Exception):
    """外部 AI provider との境界で発生する失敗の基底。"""


# 設定/前提 — コード/設定を直すまで解消しない (記事は健全なので保持)
class AIProviderConfigurationError(AIProviderError, NonRetryableKeepArticle):
    CODE: ClassVar[str] = "ai_error_configuration"

class AIProviderRequestInvalidError(AIProviderError, NonRetryableKeepArticle):
    CODE: ClassVar[str] = "ai_error_request_invalid"

class AIProviderInsufficientBalanceError(AIProviderError, NonRetryableKeepArticle):
    CODE: ClassVar[str] = "ai_error_insufficient_balance"


# レート/quota — 待機後 cron 再投入で解消
class AIProviderRateLimitedError(AIProviderError, RetryableError):
    CODE: ClassVar[str] = "ai_error_rate_limited"
    INLINE_RETRY: ClassVar[bool] = False  # 短期だが秒以上の待機が必要、cron に任せる

class AIProviderQuotaExhaustedError(AIProviderError, RetryableError):
    CODE: ClassVar[str] = "ai_error_quota_exhausted"
    INLINE_RETRY: ClassVar[bool] = False  # 時間〜日待機、cron で翌日再投入


# 一時障害 — taskiq 即時 retry が有効な可能性
class AIProviderServiceUnavailableError(AIProviderError, RetryableError):
    CODE: ClassVar[str] = "ai_error_service_unavailable"
    INLINE_RETRY: ClassVar[bool] = True

class AIProviderNetworkError(AIProviderError, RetryableError):
    CODE: ClassVar[str] = "ai_error_network"
    INLINE_RETRY: ClassVar[bool] = True


# 入力/出力 provider 明示拒否 — 永久処理不可と確信できるケース、即削除
class AIProviderInputRejectedError(AIProviderError, NonRetryableDropArticle):
    CODE: ClassVar[str] = "ai_error_input_rejected"

class AIProviderOutputBlockedError(AIProviderError, NonRetryableDropArticle):
    CODE: ClassVar[str] = "ai_error_output_blocked"
```

### Stage 固有ドメインエラー (Layer 2-B、各 stage で別ファイル)

#### Stage 3 (Extraction) — Outcome / 失敗例外の確定形

```python
# 成功種別のみ (failure は raise)
@dataclass(frozen=True, slots=True)
class ExtractedOutcome:
    """signal として成功、Stage 4 (classification) に chain。"""
    extraction: Extraction

@dataclass(frozen=True, slots=True)
class NoiseOutcome:
    """noise として成功、別テーブルに永続化済、ここで停止。"""

ExtractionOutcome = ExtractedOutcome | NoiseOutcome  # 失敗は混ぜない
```

**廃止される型** (現状実装からの差分):

| 旧 | 新 |
|---|---|
| `InvalidInputError` (Service catch → Outcome 変換) | 廃止。AI 側真の処理拒否は `AIProviderInputRejectedError` (Layer 2-A、DROP)、format 違反は `ExtractionResponseInvalidError` (Layer 2-B、RETRYABLE) に分離 |
| `InvalidInputOutcome` (失敗を Outcome に混ぜていた) | 廃止 (失敗は raise) |
| `ExtractionOutcome = ExtractedOutcome \| NoiseOutcome \| InvalidInputOutcome` | `ExtractionOutcome = ExtractedOutcome \| NoiseOutcome` |

#### 概念定義 — 「stage が必要としている情報を AI が返せなかった」

各 Stage の業務基準で「使える応答か」を判定し、不適合は Stage 固有エラーとして扱う。
provider 呼び出し自体は成功しているが、Stage が期待する形/値を満たしていないケース。

| Stage | 型 | 意味 | Layer 1 |
|---|---|---|---|
| Extraction | `ExtractionResponseInvalidError` | parse 不能、schema 違反、必須 field 抜け、entity 整合性違反など Stage 3 が必要とする抽出結果を取り出せなかった | RetryableError (inline) |
| Classification | `ClassificationResponseInvalidError` | parse 不能、schema 違反、impact_score 範囲外など Stage 4 の構造的問題 | RetryableError (inline) |
| Classification | `UnknownCategorySlugError` | AI が DB の category 集合に存在しない slug を返した (意味的問題) | RetryableError (inline) |
| Classification | `UnknownTopicSlugError` | AI が DB の topic 集合に存在しない slug を返した | RetryableError (inline) |
| Embedding | `EmbeddingResponseInvalidError` | vector dimension 不一致、空ベクトル、応答 parse 不能 | RetryableError (inline) |

> **TBD**: 上記内訳は **Stage 3 を起点に順次詰めて確定** していく。Stage 4 / 5 の
> 型は議論で増減する可能性あり (Stage 3 の議論を終えたら次に着手)。
>
> 全て `RetryableError`(INLINE_RETRY=True) で扱う方針:
> - format 違反は AI 揺らぎで retry 救済が現実的に効く
> - retry 上限到達分は記事保持 + cron TTL 救済モデル (§設計判断の根拠 参照)

#### 型定義 (現時点の骨格)

```python
# app/analysis/errors/extraction.py
class ExtractionDomainError(Exception):
    """Stage C extraction 固有のドメインエラーの基底。"""

class ExtractionResponseInvalidError(ExtractionDomainError, RetryableError):
    CODE: ClassVar[str] = "extraction_response_invalid"
    INLINE_RETRY: ClassVar[bool] = True


# app/analysis/errors/classification.py
class ClassificationDomainError(Exception):
    """Stage D classification 固有のドメインエラーの基底。"""

class ClassificationResponseInvalidError(ClassificationDomainError, RetryableError):
    CODE: ClassVar[str] = "classification_response_invalid"
    INLINE_RETRY: ClassVar[bool] = True

class UnknownCategorySlugError(ClassificationDomainError, RetryableError):
    """AI 返却 category slug が DB の category 集合に存在しない。
    現 classification/service.py:138 で `ProviderError` を流用している場所。"""
    CODE: ClassVar[str] = "unknown_category_slug"
    INLINE_RETRY: ClassVar[bool] = True

class UnknownTopicSlugError(ClassificationDomainError, RetryableError):
    CODE: ClassVar[str] = "unknown_topic_slug"
    INLINE_RETRY: ClassVar[bool] = True


# app/analysis/errors/embedding.py
class EmbeddingDomainError(Exception):
    """Stage E embedding 固有のドメインエラーの基底。"""

class EmbeddingResponseInvalidError(EmbeddingDomainError, RetryableError):
    CODE: ClassVar[str] = "embedding_response_invalid"
    INLINE_RETRY: ClassVar[bool] = True
```

**配置**: `app/analysis/errors/{provider,extraction,classification,embedding}.py`
の 4 ファイルがディレクトリで兄弟並び。`provider.py` は stage 横断で共有、
他 3 つは stage ごとに分離 (Bounded Context ごとに自分のエラーを持つ DDD 流)。

**命名規約 (Layer 2-B にも適用)**:

- 基底: `<Stage>DomainError` (例: `ClassificationDomainError`)
- 子クラス: 概念に合った名前 + `Error` サフィックス (例: `UnknownCategorySlugError`)
  — `AIProvider` のような統一プレフィックスは要らない (基底名で stage が分かる)
- CODE: stage 固有の意味を持つ snake_case (例: `unknown_category_slug`)
- 多重継承: Layer 2 (`<Stage>DomainError`) + Layer 1 marker

---

## Task 層実装 (PR3.6 完了後の姿)

### extract_content

```python
@broker_analysis.task(...)
async def extract_content(ready: ReadyForExtraction, ctx: Context = TaskiqDepends()) -> None:
    session_factory = ctx.state.session_factory
    extractor = ctx.state.extractor
    attempt = int(ctx.message.labels.get("retry_count", 0)) + 1

    svc = ExtractionService(session_factory)
    try:
        result = await svc.execute(ready, extractor)
    except NonRetryableDropArticle as exc:
        await svc.mark_article_unprocessable(
            ready.article_id, ready.original_content,
            code=type(exc).CODE, exc=exc,
        )
        return
    except NonRetryableKeepArticle as exc:
        await _audit_failure(
            session_factory, ready, exc=exc, attempt=attempt,
            category=Layer1Category.NON_RETRYABLE_KEEP_ARTICLE,
            code=type(exc).CODE,
        )
        return
    except RetryableError as exc:
        if type(exc).INLINE_RETRY and not is_last_attempt(ctx):
            raise  # taskiq 即時 retry
        await _audit_failure(
            session_factory, ready, exc=exc, attempt=attempt,
            category=Layer1Category.RETRYABLE,
            code=type(exc).CODE,
        )
        return
    except Exception as exc:
        await _audit_failure(
            session_factory, ready, exc=exc, attempt=attempt,
            category=Layer1Category.UNKNOWN,
            code="unexpected_error",
        )
        return

    # 成功経路 (Outcome は成功種別のみ、Service が同 tx で audit 焼付済)
    match result:
        case ExtractedOutcome():
            await _enqueue_classification(result, session_factory)
        case NoiseOutcome():
            return  # noise は別テーブル永続化済、ここで停止
```

→ **except 4 ブロック (3 + catch-all)**、各ブロックの中身は `type(exc).CODE` 1 行。
Outcome union は **成功種別 2 種のみ**、`InvalidInputOutcome` は廃止。

### Service 規約 — `mark_article_unprocessable` メソッド必置

各 Stage の Service には **`mark_article_unprocessable(...)` メソッドを必置** とする。
DROP_ARTICLE 経路で Task 層から呼ばれ、「audit 焼付 → 記事削除」を 1 トランザクション
で実行する責務。

```python
class ExtractionService:
    async def mark_article_unprocessable(
        self,
        *,
        article_id: int,
        original_content: str | None,
        code: str,                       # type(exc).CODE
        exc: Exception,                  # forensics 用に payload に焼く
    ) -> None:
        """DROP_ARTICLE 経路: audit を焼いてから article を削除する。

        1 トランザクションで:
        1. pipeline_events に category=non_retryable_drop_article + code を append
        2. articles テーブルから対象記事を削除 (cascade で関連レコードも削除)
        """
        ...
```

引数は **全 Stage 共通** (`article_id` + `code` + `exc` + 永続化に必要な参照)。
Service が DB 操作と audit 焼付を集約することで、Task 層は副作用の orchestration
のみに集中する。

### Service 内 (成功/skip Outcome)

```python
async def execute(...) -> ExtractionOutcome:
    response = await extractor.extract(...)

    async with self._session_factory() as session:
        repo = ExtractionRepository(session)
        event_repo = PipelineEventRepository(session)

        saved = await repo.save(...)
        outcome = ExtractedOutcome(extraction=saved)

        await event_repo.append(
            stage=Stage.EXTRACTION,
            category=_category_of(outcome),         # success
            code=type(outcome).CODE,                # extracted
            payload=ExtractionPayload(...),
            article_id=ready.article_id,
        )
        await session.commit()
        return outcome
```

---

## PR 段取り

| PR | scope | 階層変更 | schema 変更 |
|---|---|---|---|
| **PR3-a-1** (現行) | 既存階層のまま 10 except 節で audit 焼付 | なし | なし |
| **PR3.5** | Layer 1 基底クラス新設 + Layer 2 各型に `CODE` ClassVar pin + `category` / `code` / `error_class` column 追加 (nullable) | 追加のみ、behavior 不変 | 列追加 |
| **PR3.6** | Task 層 except を 3 + catch-all に集約 + `_audit_failure` を `type(exc).CODE` 経由に切替 + Stage 4/5 にも展開 | except 集約 | なし |
| **PR3.7** | `category` / `code` を NOT NULL 化、`outcome_code` を `code` の generated column 化 | なし | NOT NULL + generated |
| **PR-Future** | `unknown_category_slug` 等 stage 固有ドメインエラーを `ProviderError` 流用から正式型に移送 | Layer 2-B 新設 | なし |
| **PR3.8** (任意) | `outcome_code` DROP、`event_type` の去就を決める | なし | DROP |

---

## 設計判断の根拠 (議論履歴)

### なぜ outcome_code を独立概念から降格させるか

**「実装」と「運用」の間に立つ stable な業務語彙の辞書** という outcome_code の役割は
否定しない。だが parallel registry (ADR §12 の表) として維持すると 2 つの真実
(型階層 / ADR) のズレが起こる。

**Python 型に CODE を pin する** ことで、

- 真実は型階層 1 つ
- ADR §12 は型から導出した一覧表 (生成可能)
- 型 rename しても CODE は不変、SQL 連続性が壊れない
- 新例外追加時に CODE 命名を強制できる (型を作る瞬間に CODE を書く)

stable 性は CODE pin で構造的に保たれる。「outcome_code」という第三概念を独立に
維持する必要が消える。

### なぜ Layer 1 を 5 種にしたか

ユーザー初期提案 4 種に「環境起因 permanent (記事保持)」が欠落していた。
`AIConfigurationError` / `AIInsufficientBalance` は:

- リトライしても直らない (`RetryableError` ではない)
- 記事は健全 (`NonRetryableDropArticle` ではない)
- 人間が設定/残高を直すと再開可能

→ **5 つ目の category** が必要。これを欠くと **API key を直し忘れただけで記事が
大量削除される事故** が起きうる。

### なぜ category と code を両方 column 化するか

`category` は Layer 1 (大枠 dispatch)、`code` は Layer 2 (具体ラベル)。
それぞれ独立した SQL クエリ用途がある。片方だけだとどちらかの集計で SQL 側に
マッピングロジックを持つ羽目になる。**両方 column 化で SQL は素直** に書ける。

### なぜ `AnalysisDomainError` をリネームするか

中身が全部 AI 呼び出しインフラ起因なのに「Domain」を名乗るのは語彙の嘘。
**`AIProviderError` にリネームし**、stage 固有ドメインエラーは別ファイル
(`{stage}/errors.py`) に Layer 2-B として新設する。これにより
`unknown_category_slug` を `ProviderError` で流用する事故が構造的に消える。

### なぜ全子クラスに `AIProvider` プレフィックスを付けるか

短い名前 (`AIRateLimited` 等) との比較で議論した結果、**全子クラスに `AIProvider`
プレフィックス + `Error` サフィックス** を付けた長い形を採用。

**冗長性が実害にならない理由**:

| 場所 | 型名の出方 | 頻度 |
|---|---|---|
| 基底定義 / `raise` 文 | フルネーム必須 | 各型 1 回 + raise 数箇所 |
| Task 層 except | **登場しない** (Layer 1 marker で受ける) | 0 回 |
| import 文 | `from ... import AIProviderRateLimitedError` | 各使用ファイル 1 行 |

Task 層では `except RetryableError as exc` の形で受けるため、長い Layer 2 名は
コード本体にほぼ出ない。字面の冗長さが効く場所が実は少ない。

**ログ・監査での自己完結性が決定的に効く**:

```
"RateLimitedError"            → 何の rate limit か stacktrace だけで分からない
"AIProviderRateLimitedError"  → 外部 provider の rate limit だと自明
```

監査テーブルの `error_class` (FQN) や stacktrace に出るときに、工程固有の
`ExtractionInputTooLargeError` などと隣接して並んでも **プレフィックスで概念の
スコープが一目で分かる**。grep / dashboard 検索性も上がる (`AIProvider` で
prefix 検索すれば AI 由来を全列挙できる)。

**PEP 8 準拠**: 全例外クラスを `*Error` で統一 (Python 慣例)。

### なぜ AI provider エラーを 9 種に分けるか

「型を増やすコスト」と「観測情報量を失うコスト」を比較した結果。

- **型を増やすコスト**: 型定義 1 行 + `CODE` ClassVar 1 行 = 2 行 / 型。Layer 1
  dispatch には影響しないので Task 層の except 数も変わらない。コストはほぼゼロ
- **観測情報を失うコスト**: dashboard で「次に何をすべきか」が code から自明で
  あるべき。統合すると payload を開けないと判別できない

**判断基準**: 「次のアクションが違うなら型を分ける」。9 種すべて初動が違う:

| 型 | 起きたとき次に何をする |
|---|---|
| `AIProviderConfigurationError` | env / provider 管理画面確認、設定修正 PR |
| `AIProviderRequestInvalidError` | 直近コード変更を確認、SDK バージョン確認、修正 PR |
| `AIProviderInsufficientBalanceError` | 入金 or アダプター差替 PR |
| `AIProviderRateLimitedError` | 何もしない (cron が短期で再投入) |
| `AIProviderQuotaExhaustedError` | 何もしない、必要なら quota 増額申請 |
| `AIProviderServiceUnavailableError` | provider status page 確認、長引けば fallback 検討 |
| `AIProviderNetworkError` | 自分側のネットワーク確認、provider 側か切り分け |
| `AIProviderInputRejectedError` | 入力長 metric 確認、要約前処理の検討 |
| `AIProviderOutputBlockedError` | safety policy 変更の有無確認、別モデル検討 |

format 違反系 (`<Stage>ResponseInvalidError`) は Layer 2-B (工程エラー) に移動した
ため、ここでは含まない (詳細は §「なぜ format 違反を Layer 2-A から Layer 2-B に
移したか」参照)。

特に区別の議論があった対について:

- **Configuration vs RequestInvalid**: 両者ともコード/設定変更で解消するが、
  初動が違う (env を疑う vs コードを疑う)
- **RateLimited vs QuotaExhausted**: 両者とも待機で解消するが、待ち時間スケール
  (秒〜分 vs 時間〜日) が桁違い、運用ダッシュボードで独立観測したい
- **ServiceUnavailable vs NetworkError**: 「provider 全停止か通信路の問題か」の
  運用判断に直結 (同時多発なら provider、単発なら自分側)
- **InputRejected vs OutputBlocked**: 結論は同じ「この記事ではこのモデルで無理」
  だが、原因の所在 (入力長/内容 vs モデルの安全性判断) が違う、対処策の種類が違う

### なぜ format 違反を Layer 2-A から Layer 2-B に移したか

初版では「parse 不能 / 空応答 / schema 違反 / truncated」を `AIProviderResponseInvalidError`
として Layer 2-A に置いていたが、再改訂で削除し各 Stage の Layer 2-B
(`<Stage>ResponseInvalidError`) に分散した。理由:

**「使える応答か」の基準は Stage ごとに違う**:

| Stage | 「使える」の基準 |
|---|---|
| Stage 3 (Extraction) | 翻訳タイトル / 要約 / entities が schema 通り、entity 整合性 OK |
| Stage 4 (Classification) | category / topic slug が DB 集合に存在、impact_score が範囲内 |
| Stage 5 (Embedding) | vector dimension が一致、空でない |

provider 自体は応答できている (network / auth / rate は通っている) のに **その応答
が「特定 Stage の業務的要請」を満たしていない**、という構図。これは provider 由来
ではなく **工程由来のエラー**。

「Stage 4 で何ができていないか」を dashboard で 1 stage に集約して観測できることが
運用上の本質的価値。Layer 2-A で一括すると stage 跨ぎの dashboard になり、
「Stage 4 だけ見たい」が `WHERE stage = 'classification' AND code = 'ai_error_response_invalid'`
のような stage filter を伴う集計になる。Layer 2-B に分散すると CODE だけで stage が
特定でき、単純な GROUP BY で運用情報が取れる。

**抜けが出ない検証**:

- Stage 跨いで同じ症状 (JSON parse 不能) が発生しても、stage 視点で別 type として扱う
- dashboard で「全 stage の parse 失敗合計」を見たいときは
  `code LIKE '%response_invalid%'` で集約可能 (suffix が共通になっている)
- 評価: 抜けなし、運用も困らない

### なぜ DROP_ARTICLE を provider 明示拒否 2 種に厳密化したか

旧版では format 違反 (`AIProviderResponseInvalidError`) や `UnknownCategorySlug` も
NonRetryableDropArticle にする案があったが、再改訂で **`NonRetryableDropArticle`
は provider が明示的に処理不可と返したケースのみ** に厳密化した:

| Layer 1 | 該当する Layer 2 | 性質 |
|---|---|---|
| `NonRetryableDropArticle` (即削除) | `AIProviderInputRejectedError` / `AIProviderOutputBlockedError` の **2 種のみ** | provider が「この入力は処理不可 / この出力は出せない」と明示拒否 |

**即削除を許容する基準**:

- **Safety block (OutputBlocked)**: そもそも safety で blocked になる記事 =
  政治的暴力 / 性的 / ヘイト等の内容 → ニュース配信したくない内容と一致
- **Token 超過 (InputRejected)**: 通常のニュース記事では起きない、起きたら content
  抽出のバグの可能性、retry で記事本文は変わらない

**format 違反系を DROP しない理由**:

- AI モデルの揺らぎ (temperature > 0、構造化出力でも稀に schema を外す)
- retry で違う応答が返る可能性が現実的にある
- prompt を直すと解消する可能性
- 別モデルなら通る可能性

→ **format 違反系は `RetryableError` + `INLINE_RETRY=True`**。

### cron TTL 救済モデル — retry 上限到達分の記事の運命

`RetryableError` の retry 上限到達 (taskiq retry も尽き、cron 復旧 task でもダメ) 後
の記事は **即削除しない**。代わりに:

1. catch-all (`category=unknown`) として `pipeline_events` に記録
2. 記事自体は DB に保持
3. 別 cron job が **一定期間 (例: 30 日) 処理が完了していない記事を物理削除**

この設計の利点:

- prompt 改善 / モデル切替 / provider 増強で過去記事を救済できる時間的余地
- 「即削除に確信を持てない失敗」を時間で吸収できる
- DB 肥大は cron TTL で防げる

**実装は別 PR** で扱う。本 spec の範囲外:

- 新規 cron `cleanup_stale_articles` (TTL 経過記事の物理削除)
- `articles` の `last_pipeline_event` ベースの判定基準
- TTL 閾値の環境変数化

本 spec では「retry 上限到達分は記事保持 + 後段 cron が掃除」という前提だけ示す。

### なぜ `unknown` を型化しないか

Layer 1 marker (`RetryableError` / `NonRetryableDropArticle` / `NonRetryableKeepArticle`)
は **Task 層が `isinstance` で dispatch する** ための道具。`UnknownFailure(Exception)`
のような型を作ると、この役割と衝突する。

**「想定外を想定内型にする」矛盾**:

`UnknownFailure` を作ったとして、それを誰が `raise` するか?

- **ライブラリ / SDK / 第三者コード**: `Exception` 直接を raise する、`UnknownFailure`
  は raise してこない。
- **自分のコード**: `raise UnknownFailure(...)` する場面 = 既に「unknown」と分類できて
  いる場面 = 想定内。「想定外」の意味と矛盾する。

→ 型化した瞬間に「unknown」の意味が壊れる。**unknown であることは型では表現できない**。

**dispatch されないものを型として作る理由がない**:

```python
except NonRetryableDropArticle as exc: ...   # Layer 1 marker (dispatch)
except NonRetryableKeepArticle as exc: ...   # Layer 1 marker (dispatch)
except RetryableError as exc: ...            # Layer 1 marker (dispatch)
except Exception as exc:                     # catch-all (dispatch されなかった残余)
    category = Layer1Category.UNKNOWN
    code = "unexpected_error"
```

`unknown` は「上記 3 つの marker いずれにも `isinstance` マッチしなかった」事実を
示すラベル。これは catch-all の **後処理ラベル** であって、型階層の構成要素では
ない。

**DB レベルと Python 型レベルを分離**:

| レベル | 表現 |
|---|---|
| Python 型階層 (Layer 1 marker) | **5 種**: Exception 系 3 + Outcome 系 2 |
| DB `category` カラム値 | **6 値**: 上記 5 + `unknown` |

`Layer1Category` enum (`StrEnum`) は **DB 値の Python 表現** であり、`UNKNOWN` を
含む 6 値で正しい。Python の例外/Outcome 階層は 5 種のみ。

**利点**:

1. 型階層の一貫性: 「型は dispatch のため」という原則が崩れない
2. 誤用防止: `raise UnknownFailure(...)` という意味の壊れた書き方が構造的に不可
3. catch-all の意味が明確: 「Layer 1 marker いずれにも当てはまらなかった = 監査
   カテゴリ unknown」が `except Exception` の動作からそのまま読める

### なぜ失敗を Outcome に混ぜないか

現状 `ExtractionService.execute()` の return 型は
`ExtractedOutcome | NoiseOutcome | InvalidInputOutcome` の 3 variants で、最後の
`InvalidInputOutcome` は **失敗を Outcome union に混ぜている**。これは設計上の歪み:

**1. 「成功で止まる」と「失敗で止まる」は本質が違う**

| Outcome 種別 | 性質 | 何が起きているか |
|---|---|---|
| `ExtractedOutcome` | 成功 / 進む | signal として認識、Stage 4 へ chain |
| `NoiseOutcome` | 成功 / 止まる | noise として正常完了、別テーブル永続化済 |
| `InvalidInputOutcome` | **失敗 / 止まる** | 異常系、audit 必要、副作用 (削除 / alert) も検討要 |

成功 2 種は通常運用、失敗 1 種は異常系。1 つの union に混ぜると Task 層が
`isinstance` で「成功 or 失敗」を判定する必要が出る → Layer 1 dispatch の意味が
崩れる。

**2. Outcome 名で失敗種別を決め打ちする矛盾**

仮に `InvalidInputOutcome` を `NonRetryableKeepOutcome` のような Layer 1 寄りの
名前に変えても解決しない。理由は:

- 別の失敗型 (例: `AIProviderInputRejectedError` = DROP) を別 Outcome
  (`NonRetryableDropOutcome`) にしないといけなくなる
- 結果として Outcome union が **失敗型ごとに膨張**
- Layer 1 marker と Outcome 名で **二重管理** が発生

→ 失敗を Outcome に入れた瞬間、型システムが分裂する。

**3. retry すべき失敗を return すると retry が走らない**

`RetryableError` を Outcome として return すると Task 層は「完了」と扱う。taskiq
の retry 機構は raise を見て発火するので、return 経路では走らない。
**retryable failure は必ず raise** が構造的要請。

**4. 責務の歪み — Service が「失敗時の処理方針」を決めてしまう**

`InvalidInputOutcome()` を return する時点で「失敗だが記事は保持」という処理方針
が Service 層で固定される。本来 Service は「**何の失敗か**」を翻訳するだけで、
「**どう進めるか**」は Task 層 (Layer 1 dispatch) が決めるべき。

→ Service が `raise ExtractionResponseInvalidError(...)` するなら、Task 層が
`except RetryableError` で受けて retry 判断、最終 attempt なら audit + 記事保持を
決める。**判断の所在が正しい層に移る**。

**結論**:

- `ExtractionOutcome = ExtractedOutcome | NoiseOutcome` (成功 2 種のみ)
- `InvalidInputError` / `InvalidInputOutcome` は廃止
- AI 真の処理拒否は `AIProviderInputRejectedError` (Layer 2-A、DROP)
- format 違反は `ExtractionResponseInvalidError` (Layer 2-B、RETRYABLE)
- 失敗は全て typed exception で raise、Task 層が Layer 1 marker で dispatch

---

## 関連メモリ

- `feedback_outcome_purification.md` — Service Outcome は「次に渡す価値あるもの」のみ
- `feedback_responsibility_by_purpose.md` — 目的が違う責務は別クラス/ファイル
- `feedback_no_share_different_problems.md` — 実装が似ていても解いている問題が違うなら共用しない
- `feedback_pure_di_composition_root.md` — 概念は内側、provider 固有は composition root
- `project_pipeline_events_pr_roadmap.md` — pipeline_events PR 全体ロードマップ

## 確定事項 (2026-05-08 三改訂時点)

- **Layer 1 dispatch marker は型 5 種** (Exception 3 + Outcome 2):
  `RetryableError` / `NonRetryableDropArticle` / `NonRetryableKeepArticle` +
  `SuccessOutcome` / `IdempotentSkipOutcome`
- **DB `category` カラム値は 6 値** (型 5 種 + catch-all `unknown`)。
  `unknown` は **型階層の外** にある catch-all 監査ラベル (`UnknownFailure` 等の
  型は作らない)
- **Layer 2-A (AI provider 由来) は 9 種** (§Layer 2-A 表参照)
- **Layer 2-B (Stage 固有) は骨格のみ確定**: `<Stage>ResponseInvalidError` を各 Stage に
  必置、Classification には `UnknownCategorySlugError` / `UnknownTopicSlugError` も追加。
  Stage 3 を起点に詳細を順次詰める
- 命名規約 (Layer 2-A): `AIProvider<Concept>Error` (フルプレフィックス + Error サフィックス)
- 命名規約 (Layer 2-B): `<Stage>DomainError` 基底、子クラスは概念名 + `Error` サフィックス
- 配置: `app/analysis/errors/{provider,extraction,classification,embedding}.py` (ディレクトリ化)
- **DROP_ARTICLE は provider 明示拒否 2 種に厳密化**: `AIProviderInputRejectedError`
  / `AIProviderOutputBlockedError` のみ
- **format 違反系 (parse 不能、schema 違反、unknown slug 等) は `RetryableError`**
  + `INLINE_RETRY=True`、retry 上限到達分は記事保持 + cron TTL 救済モデルで掃除
- **Outcome は成功種別のみ** (`<Stage>Outcome` union に失敗を入れない)、**失敗は
  全て typed exception で raise**。`InvalidInputOutcome` 系は廃止
- **責務分担**: Service が provider 例外 / ValidationError を Layer 2 type に翻訳、
  Task が Layer 1 marker で dispatch
- **Service には `mark_article_unprocessable(...)` メソッド必置** (DROP_ARTICLE 経路、
  audit 焼付 + 記事削除を 1 tx で実行)
- **payload 標準 field set**: `message` / `error_class` / `attempt` /
  `validation_errors` / `raw_response` / `prompt_version` / `model` 等を
  Stage 共通 + Stage 固有で構造化 (詳細は §DB schema)
- `INLINE_RETRY` は ClassVar (Layer 1.5 サブクラス化案は不採用、型階層を増やさない)
- `outcome_code` は Layer 2 type.CODE の投影 (parallel registry を廃止)

## 残論点 (未決)

1. **工程特有エラー (Layer 2-B) の詳細** — Stage 3 は確定済 (下記 §確定事項 参照)、
   Stage 4 / 5 を順次詰める:
   - Stage 4: `UnknownTopicSlugError` を本当に独立型にするか、`InvalidImpactScoreError`
     等を追加するか (Pydantic validation で弾けるなら不要)
   - Stage 5: `EmbeddingResponseInvalidError` 1 種で十分か (vector dimension 不一致
     を独立型にする選択肢)
2. `idempotent_skip` の `event_type` を `SUCCEEDED` とするか `SKIPPED` とするか
3. `event_type` カラムを `category` 導入後に廃止するか、両方維持するか (PR3.8 で決定)
4. `_category_of` を関数ではなく `ClassVar[Layer1Category]` を Layer 1 基底に pin して
   `type(obj).LAYER1` で取る案 — Outcome 側 (dataclass) で ClassVar を継承で取れるか
   要検証
5. `error_class` を独立 column で持つか payload 内 (`payload.error_class`) に格納するか
   — DB SSoT は `code` で確定。`error_class` (`type(exc).__qualname__`) は forensics 用途
   なので column / payload どちらでも実害は小さい
6. 旧 `outcome_code` カラムとの互換期間中 (PR3.5 〜 PR3.7) の dual-write 戦略
   — 新書込で `outcome_code = code` を同値で埋める方針で良いか
7. `unknown_category_slug` (現 `classification/service.py:138` の `ProviderError`
   流用) を正式な `UnknownCategorySlugError(ClassificationDomainError, RetryableError)`
   に移送するタイミング — PR3.5 / PR3.6 / 別 PR
8. cron TTL 救済モデルの実装詳細 (TTL 閾値、判定基準、cron 名) — 別 PR で扱うが
   本 spec のどこに前提として記載するか
