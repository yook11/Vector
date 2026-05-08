# Stage 4 — Assessment 命名統一リファクタ計画

> **適用範囲**: 本リファクタは **命名の統一とパッケージ rename のみ** を対象とする。
> Stage 4 の Layer 2 例外切替・監査 row 永続化 (audit_repository 集約)・4-except dispatch
> 等の **振る舞い変更は対象外** であり、別 PR (`pipeline-events-stage4-assessment.md`)
> で扱う。

## 背景 — なぜリネームするか

### 問題 1: 層ごとに同じものを違う語彙で呼んでいる

現状コードでは Stage 4 が扱う「対象範囲外」を 3 層で異なる語彙で表現している。

| 層 | 「分類できた」 | 「対象外」 |
|---|---|---|
| AI レスポンス層 (`schema.py`) | `Classified` | `OutOfScope` |
| Service Outcome 層 (`service.py`) | `ClassifiedOutcome` | **`Rejected`Outcome** ← 言い換え |
| DB / Repository 層 | `Analysis` / `AnalysisRepository` | **`Rejection` / `RejectionRepository`** ← 更に言い換え |
| DB table | `analyses` (推定) | `rejections` (推定) |

コードを追う者は毎回「OutOfScope = Rejected = Rejection」と脳内変換させられる。

### 問題 2: `Classified` は行為を表す名前で意味が薄い

`Classified` は受身動詞 ("分類された") だが、実は `OutOfScope` のほうも「分類された」結果である
(out_of_scope というカテゴリに分類された)。両者を区別する名前としては機能していない。

→ 状態 (属性) を表す `InScope` のほうが、対称ペア `OutOfScope` と意味的に整合する。

### 問題 3: `Rejection` は能動的な行為を含意し、Stage 4 の実体と合わない

`Rejection` は「主体が却下した」というニュアンスを持つ。だが Stage 4 で起きていることは:

- AI が「先端テック領域外」と判定して `category=out_of_scope` を返す
- Service は AI の判定結果を受け取って永続化しているだけ — **却下していない**

→ 観察された属性を表す `OutOfScope` のほうが意味的に正確。

### 問題 4: `Analysis` は Stage 4 の実体としては広すぎる

`app/analysis/` パッケージ全体が analysis (Stage 1〜5 すべてが analysis の一部)。
その内側で **Stage 4 専用** entity を `Analysis` と呼ぶのは「analysis の中の analysis」
という入れ子構造になり意味が薄い。

### 問題 5: Stage 4 の実体は Classification より広い

Stage 4 が実際に行っている仕事:

```
入力: 翻訳済の title_ja + summary_ja
作業:
  1. 対象範囲判定 (in-scope / out-of-scope)        — 関連性の判断
  2. category 割り当て (ai/bio/.../other)          — 構造化分類
  3. topic 抽出 (max 3 words の自由ラベル)         — 主題抽出
  4. investor_take 生成                            — 投資視点の意味づけ
出力: 4 つの判断結果が一塊になったもの
```

仕事 1〜4 を貫く共通項は「**観察データに対して投資視点で価値判断を下す**」 — `Classification`
は仕事 2 (カテゴリ割当) のみを指し、Stage 4 の実体としては狭い。

→ 多面的な評価を含意する `Assessment` のほうが Stage 4 の実体に正確に重なる。

---

## 命名の決定 — `Assessment` × `InScope` / `OutOfScope`

### 候補比較で選定された 2 軸

**軸 1: Stage 名 (パッケージ / Service / Entity 接頭辞)**

| 候補 | 評価 |
|---|---|
| **Assessment** ★★★★★ | 採用。多面的判定 + 価値判断 + 専門領域での慣習語 |
| Appraisal ★★★★ | 二番手候補。投資寄りのニュアンスが強い |
| Interpretation ★★★ | 意味抽出に偏り、in/out-of-scope 判定の側面が弱い |
| Classification ★★ | 狭い (カテゴリ分類のみ)、現状名 |
| Analysis ✗ | 広すぎる (`app/analysis/` 全体) |
| Insight ✗ | `app/insights/` (集約 AI) と競合 |

**軸 2: 二値分岐 (in-scope / out-of-scope)**

| 候補 | 評価 |
|---|---|
| **InScope / OutOfScope** ★★★★★ | 採用。状態 (属性) を表す対称ペア |
| Classified / Rejected ★★ | 行為と能動性が混在、意味が薄い |
| InScope / Discarded ★★ | Discarded は能動的すぎる |

### 層別の最終命名マトリクス

| 層 | InScope 側 | OutOfScope 側 | 文字数の狙い |
|---|---|---|---|
| Package | `app/analysis/assessment/` | (同) | パッケージ rename |
| Stage 名 (spec) | "Stage 4: Assessment" | (同) | パイプライン文書統一 |
| AI response schema | `InScope` | `OutOfScope` | AI 境界、最短 |
| Service Outcome | `InScopeOutcome` | `OutOfScopeOutcome` | Service 戻り値、短い |
| Domain Entity (persisted) | **`InScopeAssessment`** | **`OutOfScopeAssessment`** | パッケージ外飛び、自己記述 |
| Domain Draft | `InScopeAssessmentDraft` | `OutOfScopeAssessmentDraft` | 上に同じ |
| Repository | **`InScopeRepository`** | **`OutOfScopeRepository`** | package context で短縮 |
| Service class | `AssessmentService` | (同) | Stage 名そのまま |
| Ready 型 | `ReadyForAssessment` | (同) | Stage 名統一 |
| Task 関数名 | `assess_content` | (同) | Stage 名統一 |
| Task name (broker) | `assess_content` (新) + `classify_content` (alias 一時残置) | (同) | in-flight 互換 |
| audit code | `assessed_in_scope` | `assessed_out_of_scope` | 完全対称 |
| Layer 2-B exception | `AssessmentResponseInvalidError`、`UnknownCategorySlugError` | (同) | Stage 名統一 |
| audit_repository (別 PR) | `AssessmentAuditRepository` | (同) | Stage 名統一 |
| DB table (別 PR) | `in_scope_assessments` | `out_of_scope_assessments` | 完全自己記述 |

### 命名のグラデーション設計意図

- **AI 境界 (短い)**: AI レスポンス schema は最も短く、入力境界の純粋なラベル
- **Outcome (短い)**: Service 戻り値、コードを行ったり来たりする頻度が高いので短さ優先
- **Entity (長い)**: Domain Entity はパッケージ外も飛び回るので **完全自己記述**
  (例: `app/insights/` から import される時に `InScopeAssessment` だけで意味が通る)
- **Repository (短い)**: パッケージ内で完結する寄りなので package context で短縮
  (`from app.analysis.assessment.repository import InScopeRepository` で十分)
- **Table (長い)**: SQL を psql で叩くときに table 名だけで意味が分かる必要があるので完全自己記述

### Repository 命名規約の緩和

既存規約は「**Entity 名 + Repository**」(`ExtractionRepository` / `EmbeddingRepository` 等)。
Stage 4 だけ entity 名が複合 (`InScopeAssessment`) になる事情があるため、**「同じ
package context があるなら Repository 名は短縮して良い」というルール緩和**を持ち込む。

→ Stage 5 (Embedding) も将来同様の事情が起きれば同ルール適用可。

---

## PR 分割戦略

### 全体ロードマップ

```
PR3.5-d.0 ┐ Python rename only (本 spec のスコープ)
          │   schema / outcome / domain / repository / service / task の Python 名を
          │   InScope/OutOfScope/Assessment に統一。DB schema は触らない。
          │   Task name は assess_content (新) + classify_content (alias) の二重登録。
          │
PR3.5-d.1 ┐ DB rename migration
          │   Alembic で analyses → in_scope_assessments / rejections → out_of_scope_assessments。
          │   ORM 側の __tablename__ を新名に同期。in-flight クエリへの影響を deploy
          │   ranbook で管理。
          │
PR3.5-d.2 ┐ API / frontend follow-up (必要なら)
          │   API レスポンス field 名の rename (もし露出していれば) と
          │   frontend 表示テキスト追従。型再生成 (npm run generate-types)。
          │
PR3.5-d.3 ┐ classify_content alias 削除
          │   broker queue が drain したことを確認後、compat alias を削除。
          │   独立 cleanup PR として小さく。
          │
PR3.5-d   ┐ Stage 4 振る舞いリファクタ (本 spec の対象外)
          │   Layer 2-B 例外、AssessmentAuditRepository、record_assessment_failure、
          │   4-except dispatch など。詳細は別 spec
          │   (specs/pipeline-events-stage4-assessment.md)。
```

### 分割の判断基準

| 軸 | 判断 |
|---|---|
| **rename か振る舞い変更か** | 命名統一は別 PR。「これは命名変更? 振る舞い変更?」をレビューが判別困難になるのを避ける |
| **コード変更か DB 変更か** | DB migration は本番 deploy 時の atomic 性 / rollback 性が独立した話 |
| **内向きか外向きか** | API / frontend は外向きで影響範囲・検証手順が違う |
| **同期 deploy か段階 deploy か** | alias 削除は worker queue 状態の確認が必要、cleanup として独立 |

### PR3.5-d.0 (本 spec のスコープ) の境界

**含む**:
- `app/analysis/classifier/schema.py` の `Classified` → `InScope` rename
- `app/analysis/classification/` パッケージの `app/analysis/assessment/` への rename
- `Analysis` / `AnalysisDraft` → `InScopeAssessment` / `InScopeAssessmentDraft`
- `Rejection` / `RejectionDraft` → `OutOfScopeAssessment` / `OutOfScopeAssessmentDraft`
- `AnalysisRepository` → `InScopeRepository`
- `RejectionRepository` → `OutOfScopeRepository`
- `ClassificationService` → `AssessmentService`
- `ClassifiedOutcome` / `RejectedOutcome` → `InScopeOutcome` / `OutOfScopeOutcome`
- `ReadyForClassification` → `ReadyForAssessment`
- `classify_content` 関数 → `assess_content` 関数 + `classify_content` を alias として一時残置
- 全 reference (tests / `app/insights/` / `app/digest/` / `app/search/` / `tasks.py`) の追従
- ORM `__tablename__` は **据え置き** (旧 `analyses` / `rejections` のまま)。コメントで「DB
  rename は PR3.5-d.1 で行う」と注記

**含まない**:
- DB schema 変更 / Alembic migration
- API スキーマの field 名変更 (frontend に露出している場合)
- 振る舞い変更 (Layer 2-B 例外、audit_repository、4-except dispatch 等)

### Task name alias 方式

`classify_content` (旧) と `assess_content` (新) を **同一 logic を invoke する 2 つの
task として broker に登録**する。

```python
# app/analysis/tasks.py

@broker_analysis.task(
    task_name="assess_content",
    timeout=180,
    max_retries=2,
    retry_on_error=True,
)
async def assess_content(
    ready: ReadyForAssessment,
    ctx: Context = TaskiqDepends(),
) -> None:
    """Stage 4: Assessment 本体実装 (新 task name)。"""
    # 実装はここ
    ...


@broker_analysis.task(
    task_name="classify_content",
    timeout=180,
    max_retries=2,
    retry_on_error=True,
)
async def classify_content(
    ready: ReadyForAssessment,
    ctx: Context = TaskiqDepends(),
) -> None:
    """[DEPRECATED] Compat alias for ``assess_content``.

    PR3.5-d.0 deploy 時点で broker queue に残った in-flight ``classify_content``
    message を消化するための一時 wrapper。新規 enqueue (extract_content task) は
    ``assess_content`` を使うので、本 alias 経由で新規 message が積まれることはない。

    削除条件 (PR3.5-d.3 で実施):
    - broker queue 内 ``classify_content`` task name が 0 件
    - 直近 24 時間で本関数が 1 度も invoke されていない (logfire 確認)
    - dead-letter queue に ``classify_content`` task が存在しない
    """
    logger.info(
        "classify_content_alias_invoked",
        message="this task name is deprecated, drains in-flight only",
        article_id=getattr(ready, "article_id", None),
    )
    await assess_content(ready, ctx)
```

#### in-flight message 互換性の保証

`ReadyForClassification` → `ReadyForAssessment` の rename で taskiq message の
deserialize が壊れないことを以下で担保する:

- taskiq の wire format は Pydantic の `model_dump_json()` で **field 構造ベース**
  にシリアライズされている (class qualname を持ち回らない)
- `ReadyForAssessment` は `ReadyForClassification` と **完全に同じ field 構造**
  (`extraction_id` / `translated_title` / `summary` / 他) を持つ
- alias 関数の signature が `ready: ReadyForAssessment` を受けるので、worker 側は
  そのまま新型として扱う

→ **field 名 / 型は一切変えない** ことを PR3.5-d.0 の不変条件にする。

#### alias 削除 (PR3.5-d.3) の判定基準

以下 3 つすべてを満たした時点で alias を削除する。

1. broker queue 内 `classify_content` task name が **0 件** (psql で `SELECT
   COUNT(*) FROM taskiq_redis_queue WHERE task_name = 'classify_content'` 等で確認、
   broker 実装に応じて適切な手段で)
2. 直近 24 時間で `classify_content_alias_invoked` ログが **1 件も出ていない**
   (logfire / structlog で確認)
3. dead-letter queue に `classify_content` task が **存在しない**

→ 標準的に 1〜2 週間で drain されると想定。Vector の deploy 頻度 (extract_content の
処理時間が数秒〜数十秒) を考えると、PR3.5-d.0 deploy 後 1 週間程度の運用ログを見て
判断する。

---

## 実装の不変条件 (PR3.5-d.0)

| 不変条件 | 理由 |
|---|---|
| Pydantic model の **field 名 / 型を一切変えない** | taskiq の in-flight message 互換 |
| ORM `__tablename__` は旧名 (`analyses` / `rejections`) のまま据え置き | DB rename は別 PR (PR3.5-d.1) |
| alembic migration を **追加しない** | rename と migration を別 PR で扱う方針 |
| API レスポンス field 名を **変えない** | 外向き互換は別 PR (PR3.5-d.2) |
| 振る舞い (例外 raise / catch / retry policy) を **変えない** | 振る舞い変更は別 PR (PR3.5-d) |
| audit row の出力構造を **変えない** | 監査構造の変更は別 PR (PR3.5-d) |

---

## ファイル別の変更内容 (PR3.5-d.0)

### 1. パッケージ rename: `classification/` → `assessment/`

```
mv backend/app/analysis/classification backend/app/analysis/assessment
```

ただし PR3.5-d.0 では **ファイル内のクラス名 / 関数名 / 変数名も同時に書換**。git の
rename detection で履歴は追えるが、コミットを `git mv` 単独 + 書換 commit に分ける
かは実装時の判断。

### 2. AI schema (`app/analysis/classifier/schema.py`)

```python
# Before
class Classified(BaseModel):
    ...

# After
class InScope(BaseModel):
    ...

# OutOfScope は現状維持
```

### 3. Domain Entity (`app/analysis/assessment/domain/`)

| Before | After |
|---|---|
| `domain/analysis.py` | `domain/in_scope.py` |
| `Analysis` class | `InScopeAssessment` class |
| `AnalysisDraft` class | `InScopeAssessmentDraft` class |
| `domain/rejection.py` | `domain/out_of_scope.py` |
| `Rejection` class | `OutOfScopeAssessment` class |
| `RejectionDraft` class | `OutOfScopeAssessmentDraft` class |
| `domain/ready.py::ReadyForClassification` | `domain/ready.py::ReadyForAssessment` |

### 4. Repository (`app/analysis/assessment/`)

| Before | After |
|---|---|
| `repository.py::AnalysisRepository` | `repository.py::InScopeRepository` |
| `rejection_repository.py::RejectionRepository` | `out_of_scope_repository.py::OutOfScopeRepository` |

ORM `__tablename__`:

```python
# app/models/in_scope_assessment.py (rename from analysis.py)
class InScopeAssessment(SQLModel, table=True):
    __tablename__ = "analyses"  # ← 旧名のまま据え置き、PR3.5-d.1 で rename
    ...

# app/models/out_of_scope_assessment.py (rename from rejection.py)
class OutOfScopeAssessment(SQLModel, table=True):
    __tablename__ = "rejections"  # ← 旧名のまま据え置き、PR3.5-d.1 で rename
    ...
```

### 5. Service (`app/analysis/assessment/service.py`)

```python
# Before
class ClassificationService:
    async def execute(self, ready: ReadyForClassification, ...) -> ClassificationOutcome:
        ...

ClassifiedOutcome
RejectedOutcome
ClassificationOutcome = ClassifiedOutcome | RejectedOutcome

# After
class AssessmentService:
    async def execute(self, ready: ReadyForAssessment, ...) -> AssessmentOutcome:
        ...

InScopeOutcome
OutOfScopeOutcome
AssessmentOutcome = InScopeOutcome | OutOfScopeOutcome
```

### 6. Task (`app/analysis/tasks.py`)

```python
# Before
@broker_analysis.task(task_name="classify_content", ...)
async def classify_content(ready: ReadyForClassification, ctx) -> None:
    ...

# After (assess_content + classify_content alias を 2 つ登録)
@broker_analysis.task(task_name="assess_content", ...)
async def assess_content(ready: ReadyForAssessment, ctx) -> None:
    ...

@broker_analysis.task(task_name="classify_content", ...)
async def classify_content(ready: ReadyForAssessment, ctx) -> None:
    """[DEPRECATED] Compat alias."""
    await assess_content(ready, ctx)
```

`extract_content` task 内の `classify_content.kiq()` 呼出は `assess_content.kiq()`
に書換える。

### 7. Reference 追従 (grep 全件対応)

以下を `rg` で全件 grep して機械的に追従:

```bash
rg -l "Classification\|Classified\|Rejected\|Rejection\|Analysis\|AnalysisRepository\|RejectionRepository\|ReadyForClassification\|ClassificationService\|ClassifiedOutcome\|RejectedOutcome\|classify_content" backend/
```

主な対象:
- `backend/app/analysis/tasks.py` (extract_content の chain 呼出)
- `backend/app/insights/` (集約 AI が `Analysis` / `Rejection` を読む箇所)
- `backend/app/digest/` (週次ダイジェスト pipeline)
- `backend/app/search/` (semantic search の filter)
- `backend/app/routers/` (API endpoint で `Analysis` を返す箇所、ただし wire 名は据え置き)
- `backend/app/observability/domain/payloads.py` (`ClassificationPayload` → 名前据え置き、
  PR3.5-d で振る舞い変更とともに整理)
- `backend/app/brokers.py` (DI composition root の classifier 配線、`AssessmentService` への
  rename 追従)
- `backend/tests/` 全件
- `backend/alembic/versions/*` (新規 migration なし、過去 migration の string literal は
  そのまま — DB rename は PR3.5-d.1 で行う)

### 8. テスト追加 (alias の証跡)

`backend/tests/analysis/test_assess_content_alias.py` (新規):

```python
"""classify_content (deprecated alias) の broker 登録と forward 動作を検証する。"""

import pytest
from unittest.mock import AsyncMock

from app.analysis.tasks import assess_content, classify_content


def test_alias_registered_with_legacy_task_name() -> None:
    """alias は broker に classify_content として残っている (in-flight 消化用)。"""
    assert classify_content.task_name == "classify_content"
    assert assess_content.task_name == "assess_content"


@pytest.mark.asyncio
async def test_alias_forwards_to_assess_content(monkeypatch) -> None:
    """alias 経由の invoke が新 logic を実行する。"""
    # 詳細は実装時に詰める
    ...
```

このテストの存在自体が「alias は意図的な暫定措置」のドキュメントになる。

---

## 検証コマンド (PR3.5-d.0)

### 静的解析

```bash
cd backend
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run mypy app/
```

### 単体テスト

```bash
uv run pytest tests/analysis/ -x -v          # 全 stage の analysis 系
uv run pytest tests/test_analysis_tasks.py -x -v
uv run pytest tests/analysis/test_assess_content_alias.py -x -v
uv run pytest tests/ -x -q                   # 全体回帰
```

### 名残検出 (機械チェック)

```bash
# 旧名が残っていないか確認 (alias 1 ヶ所と alembic を除いて 0 件であるべき)
rg "Classified\b|RejectedOutcome|RejectionRepository|AnalysisRepository|ClassificationService|ReadyForClassification|ClassifiedOutcome" backend/app/
rg "from app.analysis.classification" backend/  # パッケージ rename 確認
```

---

## レビュー観点 (PR3.5-d.0)

| 観点 | 確認内容 |
|---|---|
| **rename のみで振る舞い不変** | except 節の type / retry policy / audit 出力が変化していない |
| **field 名据え置き** | Pydantic model の field 名 / 型 / 順序が `ReadyForClassification` と完全に一致 |
| **ORM `__tablename__` 据え置き** | `__tablename__ = "analyses"` / `"rejections"` のまま (PR3.5-d.1 で rename 予定の旨をコメント) |
| **alembic migration なし** | 新規 migration ファイルが追加されていない |
| **alias 二重登録** | `assess_content` (新) と `classify_content` (alias) が両方 broker に登録されている |
| **alias docstring** | `[DEPRECATED]` 明記、削除条件 3 つを記述 |
| **producer 切替** | `extract_content` task 内の `.kiq` 呼出が `assess_content.kiq` に切り替わっている |
| **package rename** | `app/analysis/classification/` → `app/analysis/assessment/` (git rename detection で履歴追跡可能) |
| **API field 名据え置き** | API レスポンスの JSON field 名は `analysis` / `rejection` のまま (PR3.5-d.2 で対応予定の旨を spec に記載) |
| **frontend 影響なし** | npm run generate-types を実行しても schema diff が出ない |

---

## PR3.5-d.1 への申し送り (DB rename migration)

PR3.5-d.0 完了後、以下を別 PR で実施:

1. Alembic migration: `analyses` → `in_scope_assessments` / `rejections` → `out_of_scope_assessments`
2. `__tablename__` の旧名注記を削除し新名に同期
3. インデックス名 / 制約名 / FK 名も rename (Alembic naming convention に従う)
4. PR description に **deploy 時の rollback 手順** を明記 (テーブル名変更は段階的 deploy
   が必要、producer/consumer の同期切替を runbook 化)
5. 過去 migration ファイルの literal string は触らない (history は immutable)

## PR3.5-d.2 への申し送り (API / frontend)

PR3.5-d.0 / PR3.5-d.1 完了後:

1. API スキーマで `analysis` / `rejection` field を露出している箇所を grep
2. Pydantic schema の field 名を rename (`Field(alias="...")` で互換性維持を検討)
3. `npm run generate-types` で frontend 型再生成
4. frontend の表示文言・型 reference を rename

## PR3.5-d.3 への申し送り (alias 削除)

PR3.5-d.0 deploy 後、以下 3 条件を確認できたら alias 削除 PR を立てる:

1. broker queue 内 `classify_content` task name が 0 件
2. 直近 24 時間で `classify_content_alias_invoked` ログが 0 件
3. dead-letter queue に `classify_content` task が存在しない

→ alias 関数 + alias テスト (`test_assess_content_alias.py`) を削除する独立小 PR。

## PR3.5-d への申し送り (Stage 4 振る舞いリファクタ)

本 spec の rename PR 群が完了した後、振る舞い変更 (Stage 4 の Layer 2-B 例外 / audit_repository /
4-except dispatch 等) を別 spec `specs/pipeline-events-stage4-assessment.md` で扱う。
本 rename が **Stage 4 振る舞いリファクタの前提条件** であり、命名統一が完了してから
振る舞い変更に着手する順序を厳守する。

---

## 履歴

- 2026-05-09 初版: Stage 4 命名統一計画を独立 spec として記録 (rename と振る舞いリファクタを
  PR レベルで分離する判断)。
