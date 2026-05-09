# pipeline_events Stage 4 (Assessment) — 実装仕様

Stage 4 (assessment) の error taxonomy / audit 永続化 / Task 層 dispatch の実装確定仕様。共通基盤 (foundation Layer 1 marker / DB schema / AuditRepository パターン) は `pipeline-events-error-taxonomy.md` を参照する。Stage 3 (extraction) の同等仕様は `pipeline-events-stage3-extraction.md` を参照。

ステータス: **確定 (実装着手可)**。Stage 4 命名統一 (`stage4-assessment-rename.md`) の rename 系 PR 群 (PR3.5-d.0/d.1/d.2/d.3) 完了後の振る舞いリファクタを扱う。PR 分割方針は本 spec merge 後に別途決定。

履歴:

- 2026-05-09 初版草稿: §設計原則 / §Layer 1 marker / §例外階層 / §Outcome / §Task 層 dispatch を確定。残章 (§AuditRepository / §Service 内成功経路 / §classifier `_translate_error` / §実装ファイル一覧) は議論進行中。
- 2026-05-09 第 2 版: §AssessmentPayload / §AssessmentAuditRepository / §record_assessment_failure / §Service 成功経路 / §Stage rename / §category 追加 を確定。
- 2026-05-09 第 3 版: §Classifier 公開型 (`ClassificationRawResponse` 廃止 / `AssessmentResult` rename / `InScopeCategory` 新設) と §AssessmentCall envelope を確定。
- 2026-05-09 第 4 版: KIND-based ACL に設計刷新。`AIProviderFailureKind` enum 追加、Stage 4 派生 provider 例外 9 種を全廃止 (既存 `AIProvider*Error` を共有)、provider wrapper (`AssessmentProviderRetryableError` / `AssessmentProviderTerminalSkipError`) + `map_provider_to_assessment` mapper を導入、Layer 1 marker を `AssessmentNonRetryableKeepError` → `AssessmentTerminalSkipError` に rename、`code` / `inline_retry` を instance 属性に変更、Service.execute() を ACL boundary 化。
- 2026-05-09 第 5 版: §Classifier 実装 (`_translate_error` 戻り型 `Exception` + bare re-raise guard、DeepSeek / Gemini SDK 翻訳テーブル詳細) を確定、§実装ファイル一覧を追加、TBD クローズ。
- 2026-05-10 第 6 版 (本コミット): KIND/CATEGORY-based ACL を **tuple-based** に変更 (OpenAI evals 流の `OPENAI_TIMEOUT_EXCEPTIONS` パターン)。`provider.py` には変更を加えず、Stage 4 側に `ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS` / `ASSESSMENT_TERMINAL_SKIP_PROVIDER_ERRORS` の 2 tuple を持って `isinstance(exc, <tuple>)` で dispatch。`AssessmentRetryableError` → `AssessmentRecoverableError` rename。`inline_retry` instance attr を廃止し `provider_error: AIProviderError | None` を保持する形に変更。inline retry 判定は logfire 設計で別途検討、現状は cron 救済で統一。provider wrapper class (`AssessmentProviderRetryableError` / `AssessmentProviderTerminalSkipError`) も廃止 (marker 直接使用)。

---

## Stage 4 が扱う失敗の全体像

Stage 4 は extraction (Stage 3 で抽出された翻訳タイトル + 事実要約) を AI classifier に渡し、in-scope (投資文脈で価値あり) か out-of-scope (価値なし) を判定する。AI provider は **DeepSeek** または **Gemini** (Pure DI で `app/brokers.py` で切替、env 切替なし)。

失敗の発生源は 3 階層:

| 階層 | 出所 | 例 |
|---|---|---|
| **provider 由来** (Layer 2-A 概念、Stage 4 専用例外でラップ) | DeepSeek SDK (OpenAI 互換) / Gemini SDK / network | API key 不正、context 超過、5xx、policy block、rate limit |
| **Stage 4 工程由来** (Layer 2-B、Stage 4 固有) | response 解釈 / catalog 整合性 | `response.parsed` が `InScope`/`OutOfScope` でない、AI が catalog に無い category slug を返す |
| **想定外** (catch-all) | bug / 仕様変更 | SDK の新例外、`RuntimeError`、DB invariant 違反 (race winner missing 等) |

---

## 設計原則

本 Stage 4 spec が依拠する原則。Stage 3 spec で確立済の原則を踏襲しつつ、Stage 4 議論で追加確定したものを含む。

### 原則 1: 大分類は **処理方針** で切る (Layer 1 が業務分岐の主軸)

ビジネスロジック (Task 層) は「どう処理すべきか」で except する。「何が原因で起きたか」は audit / 観測用の詳細情報。同じ provider error でも Stage によって処理方針が変わる (例: `output_blocked` は Stage 3 では Drop、Stage 4 では Keep)。

### 原則 2: Stage 共通 marker は **作らない** (YAGNI)

「保持対象 / 復旧手順」は Stage ごとに違う:

- Stage 3: article を保持、extraction はまだ無い / 作れなかった
- Stage 4: article + extraction を保持、assessment はまだ無い / 作れなかった
- Stage 5: article + extraction + assessment を保持、embedding はまだ無い / 作れなかった

→ 同じ "keep" でも単位 / 復旧フローが違うので、無理に共通 marker を作ると意味がぼやける。Stage 4 marker は **`AssessmentError` 配下に閉じる**。foundation `RetryableError` / `NonRetryableKeepArticle` は **継承しない**。

→ 共通 marker (`NonRetryableKeepError` 等) を作るのは、Stage 5 以降で同じ形が **2 回以上出てから** 抽象化する (rule of three)。

### 原則 3: Outcome は **成功型のみ**、失敗は全て例外

Service の戻り値は「次の段階に渡す価値あるもの」のみ。失敗は raise、Outcome union に混ぜない。`feedback_outcome_purification.md` の原則に完全準拠。

→ `AssessmentOutcome = InScopeOutcome | OutOfScopeOutcome` の 2 つのみ。`IdempotentSkip` は Pattern A' (`ReadyForAssessment.try_advance_from`) で Service 到達前に止めるので Outcome 不要。

### 原則 4: provider 例外は **「何が起きたか」のみ表現** (Stage 中立、変更しない)

`AIProvider*Error` 階層 (`app/analysis/errors/provider.py`) は Stage に依存しない。`CODE` (audit ラベル) のみ持つ。Stage 3 の foundation marker 継承 (`AIProviderRateLimitedError(AIProviderError, RetryableError)` 等) と既存 `INLINE_RETRY` ClassVar は **本 spec の範囲では一切 touch しない**。Stage 別の処理方針は Stage 4 側に閉じて表現する (原則 5)。

→ provider.py に Stage 別の hint (KIND / CATEGORY enum 等) を入れない設計を選んだことで、本 spec の影響範囲は `app/analysis/assessment/` に閉じ、Stage 3 互換維持の労力がゼロになる。

### 原則 5: Stage 4 ACL — provider 例外は **boundary で Stage 4 marker に詰め替える** (tuple-based)

provider が raise する `AIProviderError` を Stage 4 の Service 層が catch し、Stage 4 側に持つ **2 つの tuple** (`ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS` / `ASSESSMENT_TERMINAL_SKIP_PROVIDER_ERRORS`) で `isinstance(exc, <tuple>)` 判定し、`AssessmentRecoverableError` / `AssessmentTerminalSkipError` のどちらかに詰め替えて re-raise する。Anti-Corruption Layer (ACL) パターン、OpenAI evals の `OPENAI_TIMEOUT_EXCEPTIONS` 流。

→ task 層は **Stage 4 marker 2 種 だけ** で dispatch (`isinstance` chain や per-error 分岐は書かない)。新規 provider 例外を追加するときは `provider_mapping.py` の該当 tuple に **1 行追加**するだけで済む (コード分岐の追加は不要)。tuple は N×M cost を完全には消さないが、追加コストを「分岐の追加」→「タプル要素の追加」に圧縮する現実解。

### 原則 6: Stage 4 で raise されうる全例外は **2 marker のいずれかを継承 / 経由**

`AssessmentResponseInvalidError` / `AssessmentCategoryMissingError` (Stage 4 specific) は marker に直接継承。provider 由来は **wrapper class を作らず**、marker を `code` + `provider_error: AIProviderError | None` の 2 instance attr で直接 instantiate して使う。これにより task 層の 2-marker dispatch が網羅的になり、wrapper class の階層を増やさない。

---

## Layer 1 marker (Stage 4 専用、大枠分岐)

Stage 4 task 層の **唯一の dispatch 軸**。Stage 4 で raise されうる全例外がこの 2 種のどちらかを継承する (provider 由来は ACL mapper 経由で marker を直接 instantiate)。foundation marker (`RetryableError` 等) は継承しない。

```python
# app/analysis/assessment/errors.py (新規)

from __future__ import annotations

from app.analysis.errors.provider import AIProviderError


class AssessmentError(Exception):
    """Stage 4 全例外の共通基底。task 層は本クラスでなく下記 2 marker を except する。"""


class AssessmentRecoverableError(AssessmentError):
    """将来の再実行で成功する可能性がある Stage 4 失敗。

    現状は taskiq の cron 救済 (単純 retry) で消化する。inline retry の判定軸は
    logfire 設計で詰める (本 spec では持たない)。

    Attributes:
        code: audit ラベル (provider 由来は ``exc.CODE`` を引き継ぎ、
            Stage 4 specific は ``"assessment_*"`` を pin)
        provider_error: provider 由来の場合は元 ``AIProviderError`` instance を保持。
            Stage 4 specific (Layer 2-B) では ``None``。
    """

    code: str
    provider_error: AIProviderError | None

    def __init__(
        self,
        message: str = "",
        *,
        code: str,
        provider_error: AIProviderError | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_error = provider_error


class AssessmentTerminalSkipError(AssessmentError):
    """リトライ無効、現状の extraction では assess できないと諦める Stage 4 失敗。

    article / extraction は保持、assessment 行は作らず audit を焼いて return。
    名前の "Terminal" は「これ以上の試行は無意味、終端」、"Skip" は「assessment
    を作らず skip する」の意。

    Attributes:
        code: audit ラベル (provider 由来は ``exc.CODE`` を引き継ぎ、
            Stage 4 specific は ``"assessment_*"`` を pin)
        provider_error: provider 由来の場合は元 ``AIProviderError`` instance を保持。
            Stage 4 specific (Layer 2-B) では ``None``。
    """

    code: str
    provider_error: AIProviderError | None

    def __init__(
        self,
        message: str = "",
        *,
        code: str,
        provider_error: AIProviderError | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_error = provider_error
```

### 設計判断

- **`code` は instance 属性**: provider 由来は元 `AIProviderError.CODE` を引き継ぎ、Stage 4 specific は固定値を pin。`pipeline_events.code` 列に直接書き込む audit ラベル
- **`provider_error: AIProviderError | None`**: provider 由来は元例外を identity 付きで保持 (audit forensics + `__cause__` 連鎖)。Stage 4 specific (Layer 2-B) は default `None` で raise (`AssessmentResponseInvalidError(message)` の内部で `super().__init__(message, code="assessment_response_invalid")` を呼ぶ形)
- **`inline_retry` は持たない**: 現状の運用は cron 救済で統一。inline retry 判定が必要になったら logfire 設計で別途検討する (本 spec の対象外)
- **provider wrapper class を作らない**: marker を直接 instantiate するので `AssessmentProviderRetryableError` / `AssessmentProviderTerminalSkipError` のような subclass は **不要**。階層を増やさない
- **`AssessmentError` 基底は task 層 dispatch には使わない**: 2 marker のどちらかで catch する規律を維持。`AssessmentError` は型階層上の祖先として保持 (Stage 4 例外の identity)
- **foundation marker (`RetryableError` 等) を継承しない**: 原則 2 (Stage 共通 marker は作らない) と整合。Stage 3 / Stage 5 と marker 階層を独立させる

### Stage 4 で Drop 系 marker を持たない理由

Stage 4 で扱う AI 応答失敗は、いずれも extraction 自体は保持して再分類 / 運用調査が妥当:

| 失敗パターン | 処理方針 |
|---|---|
| AI が summary を policy block | TerminalSkip (要約ベースなので別 model / 後日再分類で救える可能性) |
| AI が unknown category を返し続ける | TerminalSkip (category catalog 拡張で救える) |
| AI が永久に response invalid | Recoverable (cron 救済で救える、永続的なら bug 修正対象) |
| DB invariant 違反 (race winner missing 等) | catch-all (UNKNOWN ラベル) |

→ **`AssessmentNonRetryableDropError` は定義しない**。将来必要になれば追加 (現時点では yagni)。

---

## 例外階層 (Layer 2-A 共有 + Layer 2-B Stage 4 固有)

Stage 4 で raise される全例外は **Layer 1 marker (`AssessmentRecoverableError` / `AssessmentTerminalSkipError`) のいずれかを継承 / 経由**する。provider 由来 (Layer 2-A) は ACL mapper で marker を直接 instantiate、Stage 4 工程由来 (Layer 2-B) は marker を直接継承。

### Layer 2-A 共有 — `AIProviderError` 9 種 (本 spec では一切 touch しない)

`provider.py` は本 spec の範囲外で **既存のまま維持する**。Stage 別の hint (KIND / CATEGORY enum 等) は provider 側に持たせない。Stage 4 が必要とする情報 (どの provider error を recoverable / terminal-skip とみなすか) は Stage 4 側のファイルに 2 つの tuple として閉じる (§Layer 2-A → Stage 4 marker、tuple-based ACL)。

参照する既存定義 (touch せず読むだけ):

| クラス | `CODE` | foundation marker | 備考 |
|---|---|---|---|
| `AIProviderConfigurationError` | `ai_error_configuration` | `NonRetryableKeepArticle` | API key / model 名不正等 |
| `AIProviderRequestInvalidError` | `ai_error_request_invalid` | `NonRetryableKeepArticle` | request 構造不正 |
| `AIProviderInsufficientBalanceError` | `ai_error_insufficient_balance` | `NonRetryableKeepArticle` | 残高不足 (HTTP 402) |
| `AIProviderRateLimitedError` | `ai_error_rate_limited` | `RetryableError` | rate limit (HTTP 429) |
| `AIProviderQuotaExhaustedError` | `ai_error_quota_exhausted` | `RetryableError` | 日次 RPD 到達 |
| `AIProviderServiceUnavailableError` | `ai_error_service_unavailable` | `RetryableError` | provider 5xx |
| `AIProviderNetworkError` | `ai_error_network` | `RetryableError` | timeout / DNS 失敗等 |
| `AIProviderInputRejectedError` | `ai_error_input_rejected` | `NonRetryableDropArticle` | 入力 policy 違反 / safety block |
| `AIProviderOutputBlockedError` | `ai_error_output_blocked` | `NonRetryableDropArticle` | 応答 safety / recitation block |

→ Stage 3 (extraction) はこれらの foundation marker 継承で task 層 dispatch する既存挙動を維持する。Stage 4 は foundation marker を見ず、Stage 4 側の tuple で再分類する (原則 5)。

### Layer 2-A → Stage 4 marker への詰め替え (tuple-based ACL)

Stage 4 boundary で `AIProviderError` を catch して Stage 4 marker に **直接詰め替える** (provider wrapper class は作らない)。

```python
# app/analysis/assessment/provider_mapping.py (新規、Stage 4 ACL の SSoT)

from __future__ import annotations

from app.analysis.assessment.errors import (
    AssessmentError,
    AssessmentRecoverableError,
    AssessmentTerminalSkipError,
)
from app.analysis.errors.provider import (
    AIProviderConfigurationError,
    AIProviderError,
    AIProviderInputRejectedError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderQuotaExhaustedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
)


ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderNetworkError,
    AIProviderServiceUnavailableError,
    AIProviderRateLimitedError,
    AIProviderQuotaExhaustedError,
)


ASSESSMENT_TERMINAL_SKIP_PROVIDER_ERRORS: tuple[type[AIProviderError], ...] = (
    AIProviderConfigurationError,
    AIProviderRequestInvalidError,
    AIProviderInsufficientBalanceError,
    AIProviderInputRejectedError,
    AIProviderOutputBlockedError,
)


def map_provider_to_assessment(exc: AIProviderError) -> AssessmentError:
    """provider 例外を Stage 4 marker に詰め替える (Anti-Corruption Layer)。

    Stage 4 boundary (Service.execute) で呼ぶ。``AIProviderError`` の subclass で
    上記 2 tuple に未登録のものは ``TypeError``。新しい provider error 種別を
    追加したら本ファイルの tuple に必ず 1 行加える運用ルール。
    """
    if isinstance(exc, ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS):
        return AssessmentRecoverableError(
            str(exc),
            code=exc.CODE,
            provider_error=exc,
        )
    if isinstance(exc, ASSESSMENT_TERMINAL_SKIP_PROVIDER_ERRORS):
        return AssessmentTerminalSkipError(
            str(exc),
            code=exc.CODE,
            provider_error=exc,
        )
    raise TypeError(f"unmapped provider error: {type(exc).__qualname__}")
```

### Provider class → Stage 4 marker 対応表

| `AIProviderError` subclass | Stage 4 marker | 元 CODE |
|---|---|---|
| `AIProviderNetworkError` | `AssessmentRecoverableError` | `ai_error_network` |
| `AIProviderServiceUnavailableError` | `AssessmentRecoverableError` | `ai_error_service_unavailable` |
| `AIProviderRateLimitedError` | `AssessmentRecoverableError` | `ai_error_rate_limited` |
| `AIProviderQuotaExhaustedError` | `AssessmentRecoverableError` | `ai_error_quota_exhausted` |
| `AIProviderConfigurationError` | `AssessmentTerminalSkipError` | `ai_error_configuration` |
| `AIProviderRequestInvalidError` | `AssessmentTerminalSkipError` | `ai_error_request_invalid` |
| `AIProviderInsufficientBalanceError` | `AssessmentTerminalSkipError` | `ai_error_insufficient_balance` |
| `AIProviderInputRejectedError` | `AssessmentTerminalSkipError` | `ai_error_input_rejected` |
| `AIProviderOutputBlockedError` | `AssessmentTerminalSkipError` | `ai_error_output_blocked` |

→ **Stage 3 との対比**: 同じ `AIProviderInputRejectedError` でも Stage 3 は foundation `NonRetryableDropArticle` 経由で記事削除、Stage 4 は `AssessmentTerminalSkipError` で extraction 保持。Stage ごとに異なる処理方針を **Stage 4 側の tuple 定義だけで表現**でき、provider.py には Stage の概念を持ち込まない。

→ **inline retry 判定について**: 旧設計の `_INLINE_RETRY_BY_KIND` 相当のテーブルは持たない。本 spec の範囲では Recoverable は **すべて cron 救済** (近 tick での即時再試行は行わない)。logfire 設計で taskiq inline retry のチューニング方針が決まり次第、Recoverable の中で更に分類する形で別途検討する。

### Layer 2-B 固有 (Stage 4 工程由来、`assessment_*` CODE)

```python
class AssessmentResponseInvalidError(AssessmentRecoverableError):
    """AI 応答が Stage 4 schema に合致しない。

    具体的には classifier 内部の ``parse_assessment`` で:
    - 必須 key (``category`` / ``topic`` / ``investor_take``) 欠落
    - ``category`` が ``ValidCategory`` enum 外の値
    - Pydantic ValidationError (型不一致 / ``min_length`` 違反)

    AI モデルの揺らぎ (構造化出力でも稀に schema を外す) で発生、cron 救済で
    現実的に回復する見込み。
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code="assessment_response_invalid",
            provider_error=None,
        )


class AssessmentCategoryMissingError(AssessmentTerminalSkipError):
    """AI が category catalog に存在しない slug を返した。

    catalog 側の追加または prompt 側の category 列挙不一致が原因。retry しても
    AI は同じ slug を返し続けるので terminal-skip。catalog を拡張すれば解消。
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code="assessment_category_missing",
            provider_error=None,
        )
```

### 想定外 (catch-all)

`Exception` (Python 標準) — SDK の新例外 / `RuntimeError` / DB invariant 違反 (`assessment_in_scope_race_winner_missing` 等)。Stage 4 marker を継承していないため task 層の `except Exception` (catch-all) に流れ、audit に `code="unknown"` で焼付。

---

## Classifier 公開型 — `AssessmentResult` / `InScope` / `OutOfScope`

Stage 4 の AI 境界周辺の型を整理する。historical な `ClassificationRawResponse` (公開境界として中間的な flat 型) と `AssessmentResponse` (type alias) を廃止し、public surface を **「結果型 1 つ + 構成型 2 つ」** に絞る。

### 変更前 (現状)

```python
# app/analysis/classifier/schema.py (現状)

class ClassificationRawResponse(BaseModel):  # AI 境界 — flat
    category: ValidCategory   # OUT_OF_SCOPE 含む 13 種
    topic: TopicName
    investor_take: str

class InScope(BaseModel):
    category: ValidCategory   # 注: OUT_OF_SCOPE も型上 valid (型の弱さ)
    topic: TopicName
    investor_take: str

class OutOfScope(BaseModel):
    investor_take: str

AssessmentResponse = InScope | OutOfScope
```

問題点:

1. **`ClassificationRawResponse` と `InScope` の shape が同一** — 重複した中間型
2. **AI 応答 → `ClassificationRawResponse` → `InScope` の二段詰め替え** — 中間 Pydantic を経由する意味がない (parse 関数で直接 `InScope` / `OutOfScope` を構築できる)
3. **`InScope.category` で `OUT_OF_SCOPE` が型上 valid** — 「対象範囲内」を型レベルで保証できていない
4. **`Classification*` 命名** — Stage 4 命名統一 (PR3.5-d) の取りこぼし

### 変更後

```python
# app/analysis/classifier/schema.py (改修後)

from enum import StrEnum
from pydantic import BaseModel, ConfigDict, Field

from app.analysis.domain.value_objects.topic import TopicName


class ValidCategory(StrEnum):
    """AI が出力可能な category slug 全集合 (13 種、`OUT_OF_SCOPE` 含む)。

    AI への schema 提示および classifier 内部の parse 検証で使用。判定後は
    `InScopeCategory` に詰め替えるため、ドメイン側 (`InScope`) からは見えない。
    """

    AI = "ai"
    BIO = "bio"
    COMPUTING = "computing"
    ENERGY = "energy"
    MATERIALS = "materials"
    MOBILITY = "mobility"
    NETWORK = "network"
    OTHER = "other"
    ROBOTICS = "robotics"
    SECURITY = "security"
    SEMICONDUCTOR = "semiconductor"
    SPACE = "space"
    OUT_OF_SCOPE = "out_of_scope"


class InScopeCategory(StrEnum):
    """in-scope 確定後のカテゴリ slug (12 種)。`OUT_OF_SCOPE` を型レベルで除外。

    `InScope.category` の型に使うことで「対象範囲内なのに OUT_OF_SCOPE」という
    矛盾状態を型システムで排除する。
    """

    AI = "ai"
    BIO = "bio"
    COMPUTING = "computing"
    ENERGY = "energy"
    MATERIALS = "materials"
    MOBILITY = "mobility"
    NETWORK = "network"
    OTHER = "other"
    ROBOTICS = "robotics"
    SECURITY = "security"
    SEMICONDUCTOR = "semiconductor"
    SPACE = "space"
    # NO OUT_OF_SCOPE — 型レベルで排除


class InScope(BaseModel):
    """対象範囲内 (in-scope) と判定された結果。"""

    model_config = ConfigDict(frozen=True)

    category: InScopeCategory   # ← OUT_OF_SCOPE を型レベル排除
    topic: TopicName
    investor_take: str = Field(min_length=1)


class OutOfScope(BaseModel):
    """対象範囲外 (out-of-scope) — 投資判断に寄与しないと判定された結果。"""

    model_config = ConfigDict(frozen=True)

    investor_take: str = Field(min_length=1)


AssessmentResult = InScope | OutOfScope
"""Stage 4 (Assessment) の判定結果。Service はこの union を受け取り
`match` / `isinstance` で型 dispatch する。型そのものが「対象範囲内/対象範囲外」
を保証する。"""
```

### AI への要求は変更なし

Gemini / DeepSeek には引き続き **flat な response_schema** を渡す。`{category, topic, investor_take}` の 3 field を AI が埋める形で、AI に「`InScope` / `OutOfScope` のどちらかの形で返して」とは要求しない (discriminated union は AI 精度を落とすため)。

```json
{
  "category": "ai",
  "topic": "ai agents",
  "investor_take": "..."
}
```

### 振り分けは classifier 内部の parse 処理

各 classifier 実装 (`gemini.py` / `deepseek.py`) は SDK の text response から dict を取り出し、共通 parse 関数で `AssessmentResult` を構築する。AI に判別を任せず、**コード側が `category == OUT_OF_SCOPE` を見て分岐する**。

```python
# app/analysis/classifier/parse.py (新規)

from typing import Any

from pydantic import ValidationError

from app.analysis.classifier.schema import (
    AssessmentResult,
    InScope,
    InScopeCategory,
    OutOfScope,
    ValidCategory,
)
from app.analysis.domain.value_objects.topic import TopicName
from app.analysis.errors import AssessmentResponseInvalidError


def parse_assessment(payload: dict[str, Any]) -> AssessmentResult:
    """AI が返した flat dict を `AssessmentResult` に詰める。

    `category == OUT_OF_SCOPE` で `OutOfScope` に振り分け、それ以外は `InScope`。
    AI 出力のドメイン境界を 1 箇所に集約する。

    Raises:
        AssessmentResponseInvalidError: schema 違反 (key 欠落 / 型不一致 / enum 外値)
    """
    try:
        category = ValidCategory(payload["category"])
        investor_take = str(payload["investor_take"])
        if category == ValidCategory.OUT_OF_SCOPE:
            return OutOfScope(investor_take=investor_take)
        return InScope(
            category=InScopeCategory(category.value),
            topic=TopicName(str(payload["topic"])),
            investor_take=investor_take,
        )
    except (KeyError, ValueError, ValidationError) as exc:
        raise AssessmentResponseInvalidError(
            f"AI response schema mismatch: {exc}"
        ) from exc
```

### 廃止対象

| 対象 | 措置 | 理由 |
|---|---|---|
| `ClassificationRawResponse` (公開型) | 削除 | SDK 側に dict schema を直接渡せば十分、Pydantic 経由の中間型は不要 |
| `AssessmentResponse` (type alias) | `AssessmentResult` にリネーム | Stage 4 命名統一の取りこぼし、`Result` の方がドメイン的に正確 |

### 設計判断

- **AI には flat schema、code 側で discriminator 評価**: AI に discriminated union を要求すると精度低下、tagged union への詰め替えは決定的なロジック (`category == OUT_OF_SCOPE`) なのでコード側で十分
- **`InScopeCategory` 新設**: `InScope.category` が `OUT_OF_SCOPE` を型上排除 — 「対象範囲内」を型そのもので保証する設計を完徹
- **parse 関数の単一化**: provider 依存しない parse は `parse.py` に共通化、provider 固有の SDK 例外翻訳のみ各実装側 (`gemini.py` / `deepseek.py`) に分離
- **Stage 3 (`ExtractionResult`) との非対称は許容**: Stage 3 は AI 境界 = ドメインが同型 (`relevance` + `entities`)、Stage 4 は in/out 分岐の構造的差異がある — 各 Stage の出力構造に最適な型設計を採る (一律対称化は強制しない)

### 影響範囲

| 変更箇所 | 内容 |
|---|---|
| `app/analysis/classifier/schema.py` | `ClassificationRawResponse` 削除、`InScopeCategory` 追加、`InScope.category` 型変更、`AssessmentResponse` → `AssessmentResult` rename |
| `app/analysis/classifier/parse.py` (新規) | `parse_assessment()` 関数追加 |
| `app/analysis/classifier/base.py` | `_call_api()` / `classify()` の戻り型を `AssessmentResult` に変更 (実体は `AssessmentCall`、§次章) |
| `app/analysis/classifier/gemini.py` | `response_schema` を dict 直接渡し、`parse_assessment()` を呼ぶ |
| `app/analysis/classifier/deepseek.py` | 同上 |
| `app/analysis/assessment/service.py` | `InScope.category` の型が `InScopeCategory` に変わるため、category catalog 解決ロジックを調整 |
| `app/analysis/assessment/in_scope_repository.py` | DB 保存時 `category.value` (str) で書き戻す既存挙動は不変、enum 型違いで mypy が静かに通る |
| 既存 import 利用箇所 | `AssessmentResponse` → `AssessmentResult` |

---

## AssessmentCall — classifier 戻り値 envelope

`BaseClassifier._call_once()` / `classify()` の戻り値型を `AssessmentResult` から `AssessmentCall` envelope に変更する。Stage 3 (`ExtractionCall`) と同じパターン。

### 動機

Service 層で audit 焼付するために必要な情報 — AI の **raw 応答 text**、詰め替え前の **raw_category** / **raw_topic** 値、**prompt_version** — を classifier 戻り値から運び上げる必要がある。これらは `AssessmentResult` (`InScope` / `OutOfScope`) には残らない情報。とりわけ `OutOfScope` 経路では `category` / `topic` が落ちるため、「何が `out_of_scope` 判定だったのか」を audit に焼くには envelope 経由の運搬が必須。

### 構造

```python
# app/analysis/classifier/envelope.py (新規)

from dataclasses import dataclass

from app.analysis.classifier.schema import AssessmentResult


@dataclass(frozen=True, slots=True)
class AssessmentCall:
    """classifier の 1 回の API call の結果。

    Service が audit 焼付できるよう、ドメイン詰め替え後の `result` に加えて
    raw 応答情報を運ぶ。Stage 3 `ExtractionCall` と同パターン。

    Attributes:
        result: ドメイン詰め替え済みの判定結果 (`InScope` | `OutOfScope`)。
        raw_response: SDK が返した text 応答 (audit 焼付用、2KB 程度上限想定)。
        raw_category: AI が出力した category slug 値 (詰め替え前、`out_of_scope` 含む)。
        raw_topic: AI が出力した topic 文字列 (詰め替え前、`OutOfScope` 経路でも保持)。
        prompt_version: 呼び出し元 Prompt class の VERSION (8 文字 hash)。
    """

    result: AssessmentResult
    raw_response: str
    raw_category: str
    raw_topic: str
    prompt_version: str
```

### Stage 3 envelope との対称性

| 項目 | Stage 3 (`ExtractionCall`) | Stage 4 (`AssessmentCall`) |
|---|---|---|
| `result` | `ExtractionResult` (boundary = domain 同一) | `AssessmentResult = InScope \| OutOfScope` (詰め替え後) |
| `raw_response` | ◯ | ◯ |
| 個別 raw field | なし (boundary = result) | `raw_category` / `raw_topic` (詰め替え前 = audit の根拠) |
| `prompt_version` | ◯ | ◯ |
| `model_name` | なし (`BaseExtractor.MODEL` で取得) | なし (`BaseClassifier.MODEL` で取得) |

### 設計判断

- **`raw_*` field を envelope に持つ**: `OutOfScope` 経路では `result` に raw_category / raw_topic 情報が落ちるため、**audit 焼付の根拠** (= 何が `out_of_scope` 判定だったか) を運ぶには envelope 保持が必須
- **`raw_category: str` (enum でなく)**: raw は監査用、enum 化すると「妥当な値しか入らない」誤解を生む。実運用では `ValidCategory` の値が入るが、型で絞らない (もし AI が enum 外の値を返した場合は `parse_assessment` 側で `AssessmentResponseInvalidError` raise — envelope 構築時には raw 値は str として既に固定)
- **`model_name` を持たない**: Stage 3 envelope と同様、`BaseClassifier.MODEL` から取れるため duplication 不要
- **`AssessmentService.execute()` の signature 変更**: classifier から `AssessmentCall` を受け取る → 既存の `AssessmentResult` (= `InScope` | `OutOfScope`) 直接受けから差し替え

### `AssessmentPayload` field との対応

`AssessmentPayload` (§AssessmentPayload 章で確定済) の field と `AssessmentCall` の対応関係:

| `AssessmentPayload` field | 由来 |
|---|---|
| `ai_model` | `classifier.MODEL` |
| `prompt_version` | `call.prompt_version` |
| `ai_raw_response` | `call.raw_response` |
| `raw_category` | `call.raw_category` |
| `raw_topic` | `call.raw_topic` |
| `category_id` / `category_slug` / `topic` / `investor_take` | `call.result` (`InScope` の場合のみ) |
| `assessment_id` | save 後の Entity id (Service が `in_scope_repo.save()` 経由で取得) |

---

## Classifier 実装 — `_translate_error` と `_call_once`

`BaseClassifier` の単発呼び出し経路を改修する。SDK 例外を **`AIProvider*Error` まで翻訳**するのが classifier の責務、Stage 4 marker への詰め替えは Service 層 ACL が担当 (原則 5)。

### `BaseClassifier` 改修

```python
# app/analysis/classifier/base.py (改修後)

import abc
from typing import ClassVar

from app.analysis.classifier.envelope import AssessmentCall
from app.analysis.errors.provider import AIProviderError
from app.analysis.assessment.errors import AssessmentError


class BaseClassifier(abc.ABC):
    """Stage 4 — Assessment のテンプレートメソッド基底。

    Stage 3 (Extraction) の構造化出力に対して判断を下す。原文は読まない。
    判定結果は `AssessmentCall` envelope (raw_response / raw_category /
    raw_topic / prompt_version + result) で返す。
    """

    MODEL: ClassVar[str]
    RPM: ClassVar[int | None]
    RPD: ClassVar[int | None]

    # -- 抽象フック --

    @abc.abstractmethod
    async def classify(
        self,
        title_ja: str,
        summary_ja: str,
    ) -> AssessmentCall:
        """Stage 3 の出力 (title_ja + summary_ja) を判定し envelope を返す。"""
        ...

    @abc.abstractmethod
    async def _call_api(self, prompt: str) -> AssessmentCall:
        """SDK 呼び出し → `parse_assessment` → `AssessmentCall` 構築。"""
        ...

    @abc.abstractmethod
    def _translate_error(self, exc: Exception) -> Exception:
        """SDK 例外を ``AIProvider*Error`` (Stage 中立) に翻訳する。

        マップ可能なら対応する ``AIProvider*Error`` 派生 instance を返す。
        マップできなければ **入力 ``exc`` をそのまま返す** (caller が bare re-raise する規約)。
        Stage 4 marker への詰め替えは Service 層 ACL の責務であり、本メソッドは
        ``AIProvider*Error`` 段階までで停止する。
        """
        ...

    # -- 単発呼び出し --

    async def _call_once(self, prompt: str) -> AssessmentCall:
        """1 回の API call。例外を `AIProvider*Error` 階層に翻訳して raise。"""
        try:
            return await self._call_api(prompt)
        except (AIProviderError, AssessmentError):
            raise  # 既に階層内 (parse_assessment が raise した
                   # AssessmentResponseInvalidError 等含む) — 二重翻訳防止
        except Exception as exc:
            translated = self._translate_error(exc)
            if translated is exc:
                raise  # マップ未知 → catch-all 経由で UNKNOWN
            raise translated from exc
```

### 設計判断

- **`_translate_error` の戻り型は `Exception`** (現状の `AnalysisDomainError` から緩める): 翻訳できなければ入力 `exc` を return する規約 (= **bare re-raise guard**)、`raise translated from exc` で `from exc` 付きラップを避ける
- **`AIProviderError` / `AssessmentError` 配下は素通し**: classifier 内部で `parse_assessment` が raise する `AssessmentResponseInvalidError` 等は既に階層内、二重翻訳を避ける
- **classifier は Stage 4 marker を知らない**: `AssessmentRecoverableError` / `AssessmentTerminalSkipError` への詰め替えは Service 層 ACL (`map_provider_to_assessment`) — provider 中立の責務分離を維持
- **戻り型 envelope (`AssessmentCall`)**: `_call_api` / `_call_once` / `classify` 全て envelope を返す。Service が audit 焼付に必要な raw 情報を運ぶ (§AssessmentCall 参照)

### DeepSeek SDK 翻訳テーブル (`deepseek.py`)

DeepSeek は OpenAI 互換 SDK (`openai` package) を使う。`_translate_error` は以下のマップに従う。

| OpenAI SDK 例外 | 翻訳先 (`AIProvider*Error`) |
|---|---|
| `openai.AuthenticationError` | `AIProviderConfigurationError` |
| `openai.PermissionDeniedError` | `AIProviderConfigurationError` |
| `openai.NotFoundError` | `AIProviderConfigurationError` (model 名不正等) |
| `openai.BadRequestError` (HTTP 400) | `AIProviderRequestInvalidError` |
| `openai.UnprocessableEntityError` (HTTP 422) | `AIProviderRequestInvalidError` |
| `openai.RateLimitError` (HTTP 429) | `AIProviderRateLimitedError` |
| `openai.APIStatusError` (HTTP 402 = Insufficient Balance) | `AIProviderInsufficientBalanceError` |
| `openai.InternalServerError` (HTTP 5xx) | `AIProviderServiceUnavailableError` |
| `openai.APITimeoutError` | `AIProviderNetworkError` |
| `openai.APIConnectionError` | `AIProviderNetworkError` |
| `openai.APIError` (catch-all 親) | 翻訳せず exc を return (bare re-raise → catch-all UNKNOWN) |

実装 skeleton:

```python
# app/analysis/classifier/deepseek.py (一部)

import openai

from app.analysis.errors.provider import (
    AIProviderConfigurationError, AIProviderInsufficientBalanceError,
    AIProviderNetworkError, AIProviderRateLimitedError,
    AIProviderRequestInvalidError, AIProviderServiceUnavailableError,
)


class DeepSeekClassifier(BaseClassifier):
    MODEL = "deepseek-chat"

    def _translate_error(self, exc: Exception) -> Exception:
        match exc:
            case openai.AuthenticationError() | openai.PermissionDeniedError() | openai.NotFoundError():
                return AIProviderConfigurationError(str(exc))
            case openai.BadRequestError() | openai.UnprocessableEntityError():
                return AIProviderRequestInvalidError(str(exc))
            case openai.RateLimitError():
                return AIProviderRateLimitedError(str(exc))
            case openai.APIStatusError() if getattr(exc, "status_code", None) == 402:
                return AIProviderInsufficientBalanceError(str(exc))
            case openai.InternalServerError():
                return AIProviderServiceUnavailableError(str(exc))
            case openai.APITimeoutError() | openai.APIConnectionError():
                return AIProviderNetworkError(str(exc))
        return exc  # bare re-raise (UNKNOWN)
```

### Gemini SDK 翻訳テーブル (`gemini.py`)

`google-genai` SDK。`_translate_error` は status / message inspect で振り分け。

| Gemini SDK 例外 / 状態 | 翻訳先 |
|---|---|
| `errors.ClientError` (status 400, "API key not valid" 等) | `AIProviderConfigurationError` |
| `errors.ClientError` (status 400, INVALID_ARGUMENT) | `AIProviderRequestInvalidError` |
| `errors.ClientError` (status 400, content blocked) | `AIProviderInputRejectedError` |
| `errors.ClientError` (status 403) | `AIProviderConfigurationError` |
| `errors.ClientError` (status 404) | `AIProviderConfigurationError` |
| `errors.ClientError` (status 429, daily quota) | `AIProviderQuotaExhaustedError` |
| `errors.ClientError` (status 429, rate limit) | `AIProviderRateLimitedError` |
| `errors.ServerError` (status 5xx) | `AIProviderServiceUnavailableError` |
| `errors.APIError` (catch-all 親) | 翻訳せず exc を return |
| `httpx.TimeoutException` / `httpx.ConnectError` | `AIProviderNetworkError` |
| 応答 `finish_reason == SAFETY` / `RECITATION` 等 | `AIProviderOutputBlockedError` (`_call_api` 内で raise、`_translate_error` 経由しない) |

実装 skeleton:

```python
# app/analysis/classifier/gemini.py (一部)

import httpx
from google.genai import errors as genai_errors

from app.analysis.errors.provider import (
    AIProviderConfigurationError, AIProviderInputRejectedError,
    AIProviderNetworkError, AIProviderOutputBlockedError,
    AIProviderQuotaExhaustedError, AIProviderRateLimitedError,
    AIProviderRequestInvalidError, AIProviderServiceUnavailableError,
)


class GeminiClassifier(BaseClassifier):
    MODEL = "gemini-2.5-flash"

    async def _call_api(self, prompt: str) -> AssessmentCall:
        response = await self._client.aio.models.generate_content(...)
        # finish_reason チェック (出力 block は _translate_error 経由でなく直接 raise)
        finish_reason = self._extract_finish_reason(response)
        if finish_reason in {"SAFETY", "RECITATION"}:
            raise AIProviderOutputBlockedError(
                f"gemini blocked output: finish_reason={finish_reason}"
            )
        # parse_assessment 経由で AssessmentResult を得る
        ...

    def _translate_error(self, exc: Exception) -> Exception:
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
            return AIProviderNetworkError(str(exc))
        if isinstance(exc, genai_errors.ClientError):
            status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
            message = str(exc).lower()
            if status == 400:
                if "api key" in message or "permission" in message:
                    return AIProviderConfigurationError(str(exc))
                if "blocked" in message or "safety" in message:
                    return AIProviderInputRejectedError(str(exc))
                return AIProviderRequestInvalidError(str(exc))
            if status in (403, 404):
                return AIProviderConfigurationError(str(exc))
            if status == 429:
                if "quota" in message or "daily" in message:
                    return AIProviderQuotaExhaustedError(str(exc))
                return AIProviderRateLimitedError(str(exc))
        if isinstance(exc, genai_errors.ServerError):
            return AIProviderServiceUnavailableError(str(exc))
        return exc  # bare re-raise (UNKNOWN)
```

### Stage 3 (`extractor/gemini.py`) との差分

| 項目 | Stage 3 | Stage 4 |
|---|---|---|
| context length check | 必要 (生 HTML は hard cap 近接の可能性) | **不要** (extraction の title + summary は数百 token、超過しない) |
| `finish_reason == SAFETY` | `_call_api` 内 raise (= `AIProviderOutputBlockedError`) | 同左 (Stage 4 でも発生しうる、特に summary 由来) |
| `_translate_error` の戻り型 | (現状) `AnalysisDomainError` | **`Exception`** (bare re-raise guard 規約) |
| Stage 4 marker への詰め替え | 不要 (Stage 3 は foundation marker 直接利用) | Service 層 ACL で詰め替え |

---

## Outcome 確定形 — 成功種別のみ

Service の戻り値は **成功 2 種のみ**。失敗は全て raise。

```python
# app/analysis/assessment/service.py (一部)

@dataclass(frozen=True)
class InScopeOutcome:
    """In-scope と判定 → in_scope_assessments 行を作成、Stage 5 (embedding) へ chain。"""
    assessment: InScopeAssessment


@dataclass(frozen=True)
class OutOfScopeOutcome:
    """Out-of-scope と判定 → out_of_scope_assessments 行を作成、Stage 5 chain なし。"""
    assessment: OutOfScopeAssessment


AssessmentOutcome = InScopeOutcome | OutOfScopeOutcome
```

→ `IdempotentSkipOutcome` は **存在しない**。Pattern A' (`ReadyForAssessment.try_advance_from`) で Service 到達前に None を返して止める。Service が呼ばれた時点で「未 assessed」が前提。

---

## Task 層 — assess_content の 3 except dispatch

Stage 4 marker 2 種を中心に、catch-all を加えた **3 except** で網羅。`isinstance` chain や per-error 分岐は書かない。

```python
# app/analysis/tasks.py::assess_content (改修後)

@broker_analysis.task(...)
async def assess_content(ready: ReadyForAssessment, ctx: Context = TaskiqDepends()) -> None:
    session_factory = ctx.state.session_factory
    classifier = ctx.state.classifier
    svc = AssessmentService(session_factory)

    try:
        result = await svc.execute(ready, classifier)
    except AssessmentRecoverableError as exc:
        # provider 由来 (Rate / Quota / 5xx / Network 経由) または
        # Stage 4 specific (ResponseInvalid)。
        # 本 spec の範囲では inline retry は持たず、audit を焼いて return →
        # cron 救済 (taskiq schedule) で再キューを期待する。
        # inline retry を入れるかは logfire 設計で別途判断、決定したら本章を改訂。
        await record_assessment_failure(session_factory, ready, exc)
        return
    except AssessmentTerminalSkipError as exc:
        # provider 由来 (Configuration / RequestInvalid / InsufficientBalance /
        # InputRejected / OutputBlocked) または Stage 4 specific (CategoryMissing)
        await record_assessment_failure(session_factory, ready, exc)
        return
    except Exception as exc:
        # UNKNOWN: SDK 新例外 / RuntimeError / DB invariant 違反
        await record_assessment_failure(session_factory, ready, exc)
        return

    # 成功時の chain (InScope のみ Stage 5 へ)
    if isinstance(result, InScopeOutcome):
        async with session_factory() as session:
            embedding_repo = EmbeddingRepository(session)
            ready_emb = await ReadyForEmbedding.try_advance_from(
                result.assessment,
                embedding_repo,
            )
        if ready_emb is not None:
            await generate_embedding.kiq(ready_emb)
```

### 設計判断

- **inline retry を持たない**: 本 spec の範囲では Recoverable はすべて cron 救済で消化する。`exc.inline_retry` instance attr / `type(exc).INLINE_RETRY` ClassVar のいずれも参照しない。logfire 設計で taskiq inline retry のチューニング方針が決まり次第、本章に inline retry hook を追加する想定
- **provider 由来は Service.execute で詰め替え済み**: task 層に届く時点で `AssessmentRecoverableError` / `AssessmentTerminalSkipError` のいずれか、または `Exception` (catch-all)
- **3-except に集約**: Stage 4 では Drop 系を持たない (extraction を捨てない)、Recoverable / TerminalSkip / catch-all で十分
- **`AIProviderError` を task 層で見ない**: ACL boundary は Service 層、task 層は Stage 4 marker のみ知る

### Stage 3 (extract_content) との差分

| 項目 | Stage 3 (extract_content) | Stage 4 (assess_content) |
|---|---|---|
| except 数 | 4 (Drop / Keep / Retryable / catch-all) | **3 (Retryable / TerminalSkip / catch-all)** |
| Drop 経路 | `mark_article_unprocessable` (audit + DELETE) | **無し** (Stage 4 では article / extraction を捨てない) |
| Layer 1 marker | foundation `RetryableError` / `NonRetryableKeepArticle` / `NonRetryableDropArticle` | Stage 4 専用 `AssessmentRecoverableError` / `AssessmentTerminalSkipError` |
| provider 例外の経路 | foundation marker 継承で task 層に直接届く | Service 層 ACL (`map_provider_to_assessment`) で Stage 4 marker に詰め替え |
| audit 関数 | `record_extraction_failure` | `record_assessment_failure` (新規、別ファイル) |
| `inline_retry` 判定 | `type(exc).INLINE_RETRY` (ClassVar) | 本 spec では持たない (cron 救済で統一、logfire 設計で別途) |

---

## AssessmentPayload — Stage 4 監査 row の payload 構造

`app/observability/domain/payloads.py` 内に定義される `BasePipelineEventPayload` の Stage 4 派生型。Stage 3 の `ExtractionPayload` が確立した責務分離パターンを踏襲。

### 設計方針

- **payload は詳細情報のみを持つ** — top-level column と重複する情報 (`article_id` / `category` / `code` / `outcome_code` / `error_class` / `attempt` / `occurred_at`) は **一切入れない**
- **状態識別は top-level column で完結** — `event_type` / `outcome_code` / `category` / `code` の 4 軸で in-scope / out-of-scope / failure / unknown を区別する。payload 内で再表現しない (二重化禁止、foundation 原則 1「監査行は型から projection」と整合)
- foundation `pipeline-events-error-taxonomy.md` の payload 設計指針 (`Pydantic discriminated union` 推奨) は Stage 別 payload variant という形で踏襲。Stage 4 は `kind="assessment"` で discriminate

### Field 完全リスト

```python
# app/observability/domain/payloads.py (Stage 4 派生型)

class AssessmentPayload(BasePipelineEventPayload):
    """Stage 4 (assessment) の payload variant。"""

    kind: Literal["assessment"] = "assessment"

    # ─── Stage 4 固有 identifier (top-level column が無いため payload で保持) ───
    extraction_id: int | None = None

    # ─── A 級: メタデータ ───────────────────────────────────────
    ai_model: str | None = None              # 使用 classifier の model 名
    prompt_version: str | None = None        # prompt+model+config の SHA-256 prefix 8

    # ─── A' / S 級: AI 入出力 (ADR §AI raw I/O 捕捉ポリシー) ────
    # Stage 4 = input full 4KB + raw 2KB
    input_text: str | None = None            # 入力 summary 全文 (4KB 上限)
    input_text_length: int | None = None     # truncate 検知用
    ai_raw_response: str | None = None       # AI raw JSON response (2KB 上限)

    # ─── A 級: AI 応答の生メタデータ (validation 前) ────────────
    # response_invalid 失敗 forensics 用
    raw_category: str | None = None          # AI が返した未検証 category slug
    raw_topic: str | None = None             # AI が返した topic 文字列

    # ─── A 級: 成功時の永続化結果ミラー (failure 時は None) ─────
    assessment_id: int | None = None
    category_id: int | None = None
    category_slug: str | None = None         # category catalog 確認後の slug
    topic: str | None = None                 # 永続化された TopicName
    investor_take: str | None = None         # in-scope のときのみ
```

### Base からの継承 field

`BasePipelineEventPayload` から継承される共通 field (どの Stage でも同じ意味):

- `kind: str` — discriminator (Stage 4 では `"assessment"` で pin)
- `source_name: str | None` — FK 切断耐性 (`news_sources.id` SET NULL されても source 名を保持)
- `error_message: str | None` — 失敗時の例外メッセージ (2000 文字上限)
- `error_chain: list[str] | None` — exception chain の FQN リスト (深さ上限 8)

### 状態識別ルール (top-level column のみで完結)

| 状態 | event_type | outcome_code | category | code | payload で non-None になる主 field |
|---|---|---|---|---|---|
| in-scope 成功 | `succeeded` | `assessed_in_scope` | `success` | `assessed_in_scope` | `assessment_id` / `category_id` / `category_slug` / `topic` / `investor_take` |
| out-of-scope 成功 | `succeeded` | `assessed_out_of_scope` | `success` | `assessed_out_of_scope` | `assessment_id` のみ (out-of-scope は in-scope 系 field を持たない) |
| 失敗 (Layer 2-A) | `failed` | `ai_error_*` | `retryable` / `non_retryable_keep_extraction` | `ai_error_*` | `error_message` / `error_chain` (Base) + `ai_raw_response` (該当時) |
| 失敗 (Layer 2-B) | `failed` | `assessment_*` | `retryable` / `non_retryable_keep_extraction` | `assessment_*` | 同上 |
| 失敗 (catch-all) | `failed` | `unexpected_error` | `unknown` | `unexpected_error` | 同上 |

→ payload 内で「どの状態か」を判別する field は **持たない**。state は top-level column で完全識別可能。

### Stage 3 `ExtractionPayload` との差分

| 項目 | Stage 3 | Stage 4 |
|---|---|---|
| `kind` | `"extraction"` | `"assessment"` |
| 識別子 | (top-level の `article_id` のみで十分) | `extraction_id` を payload に保持 (top-level 無し) |
| 結果ミラー field | `entity_count` | `assessment_id` / `category_id` / `category_slug` / `topic` / `investor_take` |
| 入力捕捉 | `input_content_length` / `input_content_head` (2KB) / `input_content_hash` | `input_text` (4KB full) / `input_text_length` (ADR §AI raw I/O 捕捉ポリシーの Stage 別差分) |

---

## AssessmentAuditRepository — 監査永続化の SSoT

`app/analysis/assessment/audit_repository.py` (新規)。Service / Task は `PipelineEventRepository.append()` を **直接呼ばない**。本 class の semantic method を呼ぶだけで、`AssessmentPayload` の組み立て・`error_chain` の FQN 構築・`category` / `code` の決定を一切知らない。

tx 境界は呼出側が握る (本 class は `commit` を呼ばない)。

### 3 semantic methods

| method | 用途 | category | code | 呼ばれる場所 |
|---|---|---|---|---|
| `append_in_scope(*, ready, envelope, assessment, code)` | in-scope 成功 | `success` | caller 渡し (`"assessed_in_scope"`) | Service `_handle_in_scope` 内、業務 INSERT と同 tx |
| `append_out_of_scope(*, ready, envelope, assessment, code)` | out-of-scope 成功 | `success` | caller 渡し (`"assessed_out_of_scope"`) | Service `_handle_out_of_scope` 内、同 tx |
| `append_failure(*, ready, exc, attempt)` | Retryable / NonRetryableKeep / catch-all | exc から自動導出 | exc から自動導出 | Task 層 `record_assessment_failure` 経由、別 session 別 tx |

→ Stage 3 の 4 method (extracted/noise/drop_article/failure) と比較して **Drop method なし** が唯一の構造差分。

### 内部実装

```python
class AssessmentAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = PipelineEventRepository(session)  # generic SQL を委譲

    async def append_in_scope(
        self,
        *,
        ready: ReadyForAssessment,
        envelope: AssessmentCall,    # classifier `_call_once` 戻り値
        assessment: InScopeAssessment,
        code: str,
    ) -> None:
        source_name = await self._resolve_source_name(ready.article_id)
        payload = AssessmentPayload(
            kind="assessment",
            source_name=source_name,
            extraction_id=ready.extraction_id,
            ai_model=envelope.model_name,
            prompt_version=envelope.prompt_version,
            input_text=ready.summary[:_INPUT_TEXT_LIMIT],
            input_text_length=len(ready.summary),
            ai_raw_response=envelope.raw_response[:_AI_RAW_RESPONSE_LIMIT],
            raw_category=envelope.raw_category,
            raw_topic=envelope.raw_topic,
            assessment_id=assessment.id,
            category_id=assessment.category_id,
            category_slug=envelope.raw_category,
            topic=str(assessment.topic),
            investor_take=assessment.investor_take,
        )
        await self._events.append(
            stage=Stage.ASSESSMENT,
            event_type=EventType.SUCCEEDED,
            outcome_code=code,
            payload=payload,
            article_id=ready.article_id,
            category=Layer1Category.SUCCESS,
            code=code,
        )

    async def append_out_of_scope(
        self,
        *,
        ready: ReadyForAssessment,
        envelope: AssessmentCall,
        assessment: OutOfScopeAssessment,
        code: str,
    ) -> None:
        # in-scope と同型、ただし category_id / topic / investor_take は None
        ...

    async def append_failure(
        self,
        *,
        ready: ReadyForAssessment,
        exc: BaseException,
        attempt: int,
    ) -> None:
        source_name = await self._resolve_source_name(ready.article_id)
        category = self._category_of(exc)
        code = self._code_of(exc)
        payload = AssessmentPayload(
            kind="assessment",
            source_name=source_name,
            extraction_id=ready.extraction_id,
            error_message=str(exc)[:_ERROR_MESSAGE_LIMIT] or None,
            error_chain=[_fqn(exc)],
            ai_raw_response=getattr(exc, "raw_response", None),  # parse 失敗 forensics
        )
        await self._events.append(
            stage=Stage.ASSESSMENT,
            event_type=EventType.FAILED,
            outcome_code=code,
            payload=payload,
            article_id=ready.article_id,
            attempt=attempt,
            error_class=_fqn(exc),
            category=category,
            code=code,
        )

    @staticmethod
    def _category_of(exc: BaseException) -> Layer1Category:
        # Stage 4 では `AssessmentTerminalSkipError` を Layer1Category.NON_RETRYABLE_KEEP_EXTRACTION
        # にマップ (extraction を捨てない、article 保持の最も保守的な意味)
        if isinstance(exc, AssessmentTerminalSkipError):
            return Layer1Category.NON_RETRYABLE_KEEP_EXTRACTION  # ← 新値 (本 PR で追加)
        if isinstance(exc, AssessmentRecoverableError):
            return Layer1Category.RETRYABLE
        return Layer1Category.UNKNOWN

    @staticmethod
    def _code_of(exc: BaseException) -> str:
        # Stage 4 marker は instance attribute で `code` を持つ規約 (Layer 1 marker §)
        code = getattr(exc, "code", None)
        return code if isinstance(code, str) and code else "unexpected_error"
```

### 設計判断

- **`append_in_scope` / `append_out_of_scope` を別 method に分離**: 内容は payload field の一部が違うだけだが、意味 (in-scope vs out-of-scope) が違うため caller が outcome 種別ごとに呼び分ける
- **`append_failure` のみ exc から自動導出**: Task 層 3-marker dispatch (`AssessmentRecoverableError` / `AssessmentTerminalSkipError` / `Exception`) の共通化、isinstance 分岐 + instance 属性 (`exc.code`) 抽出を 1 箇所に集約
- **`PipelineEventRepository` を compose**: generic な append SQL は generic repo に委譲、本 class は Stage 4 固有の payload shape / category / code 決定だけを担う
- **commit しない**: tx 境界は caller (Service `_handle_*` / `record_assessment_failure`) が握る、同一 tx 必須経路 (in_scope / out_of_scope) と別 tx 経路 (failure-only) を caller 側で出し分ける
- **`AssessmentCall`**: classifier の `_call_once` が返す envelope (raw_response / raw_category / raw_topic / prompt_version を抱える)。詳細は §AssessmentCall 章および §Classifier 実装 章を参照

---

## record_assessment_failure — Task 層 application helper

`app/analysis/assessment/failure_recording.py` (新規)。業務 tx が rollback された後に **別 session で別 tx** として audit を焼く。Stage 3 の `record_extraction_failure` と同型。

```python
async def record_assessment_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ready: ReadyForAssessment,
    exc: BaseException,
    attempt: int,
) -> None:
    """Stage 4 failure を pipeline_events に焼付ける (Task 層から呼ぶ)。

    audit INSERT 自体に失敗した場合は exception を吞んで warning ログを残す
    (audit 失敗で業務 task まで死なせない)。
    """
    try:
        async with session_factory() as session:
            await AssessmentAuditRepository(session).append_failure(
                ready=ready, exc=exc, attempt=attempt,
            )
            await session.commit()
    except Exception as audit_exc:
        logger.exception(
            "assessment_failure_audit_dropped",
            extraction_id=ready.extraction_id,
            article_id=ready.article_id,
            original_exc_class=type(exc).__name__,
            audit_exc_class=type(audit_exc).__name__,
        )
```

### 設計判断

- `Stage.ASSESSMENT` の hardcode は Stage 4 専用 helper であることを名前 (`record_assessment_failure`) で表現済
- audit INSERT 失敗を吞む方針は Stage 3 `record_extraction_failure` / foundation `_record_failure_event` と同じ。「audit 失敗で業務 task の retry を増やさない」原則
- 業務 task が retry すると本 helper は何度も呼ばれる (毎 attempt 1 行 audit)。`attempt` 引数で何回目の試行かを記録

---

## Service 内 — ACL boundary + 成功経路 (同 tx audit 焼付)

`app/analysis/assessment/service.py` を 2 点改修する:
1. **ACL boundary**: classifier が raise した `AIProviderError` を catch し、`map_provider_to_assessment` で Stage 4 marker に詰め替えて re-raise
2. **同 tx audit**: 業務 INSERT (`in_scope_assessments` / `out_of_scope_assessments` 行) と audit INSERT を同一 tx で commit

### `execute()` の ACL boundary

```python
class AssessmentService:
    async def execute(
        self,
        ready: ReadyForAssessment,
        classifier: BaseClassifier,
    ) -> AssessmentOutcome:
        """Stage 4 boundary。classifier 由来の provider 例外を Stage 4 marker に詰め替える。"""
        try:
            call = await classifier.classify(...)  # AssessmentCall を受ける
        except AIProviderError as exc:
            raise map_provider_to_assessment(exc) from exc
        # ↑ AssessmentResponseInvalidError / AssessmentCategoryMissingError 等
        #   Stage 4 specific 例外は classifier 内 / Service 内で raise されたまま
        #   (既に Stage 4 marker のため詰め替え不要)

        match call.result:
            case InScope():
                return await self._handle_in_scope(...)
            case OutOfScope():
                return await self._handle_out_of_scope(...)
```

### `_handle_in_scope` / `_handle_out_of_scope`

```python
class AssessmentService:
    async def _handle_in_scope(
        self,
        session: AsyncSession,
        *,
        ready: ReadyForAssessment,
        envelope: AssessmentCall,
        in_scope: InScope,
        model_name: str,
    ) -> InScopeOutcome:
        # 1. category catalog 確認 (失敗時は AssessmentCategoryMissingError raise)
        category_id = await self._resolve_category_id(session, in_scope.category)
        if category_id is None:
            raise AssessmentCategoryMissingError(
                f"AI returned unknown category slug: {in_scope.category!r}"
            )

        # 2. in_scope_assessments 行を構築 → save (楽観的ロック付き INSERT)
        draft = InScopeAssessmentDraft(...)
        in_scope_repo = InScopeRepository(session)
        saved = await in_scope_repo.save(draft, extraction_id=ready.extraction_id)

        # 3. レース敗北時は winner を読み戻し (audit は焼かずに idempotent skip)
        if saved is None:
            saved = await in_scope_repo.find_by_extraction_id(ready.extraction_id)
            if saved is None:
                raise RuntimeError("assessment_in_scope_race_winner_missing: ...")
            # race 敗北は「すでに別 worker が焼いた」と解釈、二重 audit を避ける
            return InScopeOutcome(assessment=saved)

        # 4. 同 tx で audit append (業務 INSERT と同じ commit で確定)
        audit_repo = AssessmentAuditRepository(session)
        await audit_repo.append_in_scope(
            ready=ready,
            envelope=envelope,
            assessment=saved,
            code="assessed_in_scope",
        )
        await session.commit()

        return InScopeOutcome(assessment=saved)

    async def _handle_out_of_scope(
        self,
        session: AsyncSession,
        *,
        ready: ReadyForAssessment,
        envelope: AssessmentCall,
        out_of_scope: OutOfScope,
        model_name: str,
    ) -> OutOfScopeOutcome:
        # _handle_in_scope と同型、ただし category 解決なし、append_out_of_scope を呼ぶ
        ...
```

### 設計判断

- **同 tx で audit append**: 業務 INSERT (`in_scope_assessments` 行) と audit INSERT (`pipeline_events` 行) が **同じ commit** で確定する → 「audit が焼けた = 業務反映が確定した」という不変条件が DB レベルで保証される
- **race 敗北時は audit 焼かない**: 楽観的ロック敗北 (`saved is None`) は idempotent skip 相当で「すでに別 worker が焼いた」と解釈、二重 audit を避ける
- **`AssessmentCategoryMissingError` は audit 経路で焼く**: Service が raise → Task 層 `except AssessmentTerminalSkipError` で `record_assessment_failure` 経由で焼かれる (失敗経路、別 tx)
- **`AssessmentService.execute` の signature 変更**: classifier から `AssessmentCall` を受け取り、provider 例外は Service 層で Stage 4 marker に詰め替え (ACL boundary)
- **provider mapping の SSoT は Service 層**: classifier は Stage 中立を保ち、`AIProviderError` のみ raise する。Stage 4 への詰め替えは `map_provider_to_assessment` 経由

### Stage 3 (`_persist_signal` / `_persist_noise`) との差分

| 項目 | Stage 3 | Stage 4 |
|---|---|---|
| 同 tx audit | ✅ | ✅ (mirror) |
| race 敗北時 | retry winner 読戻し | idempotent skip (audit 焼かない) |
| envelope 受け渡し | `extract` 戻り値が envelope | classifier `_call_once` 戻り値が envelope (§Classifier 実装 確定済) |

---

## Stage enum 値 rename 戦略 — (α) 一括 migration

Stage 4 命名統一 PR3.5-d.0/d.1/d.2/d.3 では rename 対象外だった `Stage.CLASSIFICATION = "classification"` を **本 PR で `"assessment"` に rename**。enum / DB CHECK 制約 / 既存 row / `payload.kind` 全てを 1 PR で書き換える。

### Python enum 改修

```python
# app/observability/categories.py (改修前)
class Stage(StrEnum):
    ...
    CLASSIFICATION = "classification"
    ...

# 改修後
class Stage(StrEnum):
    ...
    ASSESSMENT = "assessment"
    ...
```

### Migration 手順

```python
# alembic/versions/sN_pe_classification_to_assessment.py (新規)

def upgrade() -> None:
    # 1. CHECK 制約から旧値削除 + 新値追加
    op.execute("ALTER TABLE pipeline_events DROP CONSTRAINT ck_pipeline_events_stage")
    op.execute("""
        ALTER TABLE pipeline_events ADD CONSTRAINT ck_pipeline_events_stage CHECK (
            stage IN (
                'dispatch', 'source_fetch', 'content_fetch',
                'extraction', 'assessment', 'embedding',
                'backfill_extract', 'backfill_classify', 'backfill_embed'
            )
        )
    """)

    # 2. 既存 row を一括書き換え
    op.execute("""
        UPDATE pipeline_events
        SET stage = 'assessment'
        WHERE stage = 'classification'
    """)

    # 3. payload.kind も同時に書き換え (JSONB 内、discriminated union 整合性維持)
    op.execute("""
        UPDATE pipeline_events
        SET payload = jsonb_set(payload, '{kind}', '"assessment"')
        WHERE payload->>'kind' = 'classification'
    """)


def downgrade() -> None:
    # 完全な逆操作 (defensive)
    op.execute("""
        UPDATE pipeline_events
        SET payload = jsonb_set(payload, '{kind}', '"classification"')
        WHERE payload->>'kind' = 'assessment'
    """)
    op.execute("""
        UPDATE pipeline_events
        SET stage = 'classification'
        WHERE stage = 'assessment'
    """)
    op.execute("ALTER TABLE pipeline_events DROP CONSTRAINT ck_pipeline_events_stage")
    op.execute("""
        ALTER TABLE pipeline_events ADD CONSTRAINT ck_pipeline_events_stage CHECK (
            stage IN (
                'dispatch', 'source_fetch', 'content_fetch',
                'extraction', 'classification', 'embedding',
                'backfill_extract', 'backfill_classify', 'backfill_embed'
            )
        )
    """)
```

### 設計判断

- **(β) 並存案を却下した理由**: dashboard クエリで `WHERE stage IN ('classification', 'assessment')` のような書き方が必要、運用負担が永続化する
- **歴史的記録 (「当時は classification だった」) の喪失問題**: `git log` と本 spec の履歴で記録は残る。pipeline_events は append-only 監査ログだが、`stage` 名は「Stage 4 という概念単位」を指す label であり、当時の呼称の保持自体には大きな価値はないと判断
- **`payload.kind` も同時書き換え**: discriminated union の整合性を維持。`jsonb_set` で個別更新可能
- **`backfill_classify`** は据え置き: backfill stage の呼称 rename はスコープ拡大、別議題で扱う (もしくは PR3.5-e で Stage 5 と同時に整理)

### Migration 規模見積

- `pipeline_events` で `stage='classification'` の行数 (PR3.5-c deploy 後の累計、現状で数万行オーダー想定)
- UPDATE 速度: BRIN index + GIN index の影響あり、stage 列単独 UPDATE は数秒〜数十秒見込み
- 本番では deploy window で実行、stage='classification' 期間中の analytics は migration 後に再集計する想定

---

## category 値の追加 — non_retryable_keep_extraction

`Layer1Category` enum と `pipeline_events.category` CHECK 制約に新値 `non_retryable_keep_extraction` を追加。Stage 4 marker `AssessmentTerminalSkipError` をこの category にマップ (DB 横串 query では他 Stage の `non_retryable_keep_*` と family 検索可能、marker 名と DB 命名は別軸)。

### 命名根拠

`non_retryable_keep_*` の `*` 部分は **その Stage で保持される最深部** を指す:

| Stage | category 値 | 保持される最深部 | 作れなかったもの |
|---|---|---|---|
| Stage 3 (extraction) | `non_retryable_keep_article` | article | extraction |
| **Stage 4 (assessment)** | **`non_retryable_keep_extraction`** | **extraction (+ article)** | **assessment** |
| Stage 5 (embedding、将来) | `non_retryable_keep_assessment` | assessment (+ extraction + article) | embedding |

→ Stage ごとに別 category 値を持つ (原則 2: Stage 共通 marker は作らない、と整合)。Stage 5 でも同型パターンを踏襲。

### Layer1Category enum 追加

```python
# app/observability/categories.py

class Layer1Category(StrEnum):
    SUCCESS = "success"
    IDEMPOTENT_SKIP = "idempotent_skip"
    RETRYABLE = "retryable"
    NON_RETRYABLE_DROP_ARTICLE = "non_retryable_drop_article"        # Stage 3 既存
    NON_RETRYABLE_KEEP_ARTICLE = "non_retryable_keep_article"        # Stage 3 既存
    NON_RETRYABLE_KEEP_EXTRACTION = "non_retryable_keep_extraction"  # ← Stage 4 新規追加
    UNKNOWN = "unknown"
```

### CHECK 制約 update

Stage rename migration と **同じ revision に同梱可能** (両方とも CHECK 制約 update + 既存 row 関連)。実装時 PR スコープを見て統合判断:

```python
# alembic/versions/sN_pe_classification_to_assessment.py (上記 Stage rename と同 revision)

def upgrade() -> None:
    # ... (Stage rename の手順) ...

    # 4. category CHECK 制約 update (新値追加)
    op.execute("ALTER TABLE pipeline_events DROP CONSTRAINT ck_pipeline_events_category")
    op.execute("""
        ALTER TABLE pipeline_events ADD CONSTRAINT ck_pipeline_events_category CHECK (
            category IS NULL
            OR category IN (
                'success',
                'idempotent_skip',
                'retryable',
                'non_retryable_drop_article',
                'non_retryable_keep_article',
                'non_retryable_keep_extraction',
                'unknown'
            )
        )
    """)
```

→ 推奨: **同 revision に同梱** (deploy 簡素化、両方とも Stage 4 振る舞いリファクタの不可分要素)。

---

## 実装ファイル一覧 (本 PR 群でタッチする境界)

新規 / 改修ファイルの全体像。PR 分割は **本 spec の merge 後に別途確定**するが、最低限の内訳目安を記す。

### 新規ファイル (Stage 4 specific)

| パス | 内容 |
|---|---|
| `app/analysis/assessment/errors.py` | `AssessmentError` / `AssessmentRecoverableError` / `AssessmentTerminalSkipError` (Layer 1 marker、`code` + `provider_error: AIProviderError \| None` の 2 instance attr) + Layer 2-B 固有 (`AssessmentResponseInvalidError` / `AssessmentCategoryMissingError`、`provider_error=None` で marker を直接継承) |
| `app/analysis/assessment/provider_mapping.py` | `ASSESSMENT_RECOVERABLE_PROVIDER_ERRORS` / `ASSESSMENT_TERMINAL_SKIP_PROVIDER_ERRORS` の 2 tuple + `map_provider_to_assessment` (tuple-based ACL の SSoT、`isinstance(exc, <tuple>)` で dispatch) |
| `app/analysis/assessment/audit_repository.py` | `AssessmentAuditRepository` (`append_in_scope` / `append_out_of_scope` / `append_failure`) |
| `app/analysis/assessment/failure_recording.py` | `record_assessment_failure` (Task 層 helper) |
| `app/analysis/classifier/envelope.py` | `AssessmentCall` envelope dataclass |
| `app/analysis/classifier/parse.py` | `parse_assessment(payload: dict) -> AssessmentResult` (AI 応答 dict → ドメイン詰め替え) |

### 改修ファイル

| パス | 内容 |
|---|---|
| `app/analysis/classifier/schema.py` | `ClassificationRawResponse` 削除、`InScopeCategory` enum 新設、`InScope.category` 型変更、`AssessmentResponse` → `AssessmentResult` rename |
| `app/analysis/classifier/base.py` | 戻り型を `AssessmentCall` に変更、`_translate_error` 戻り型を `Exception` に緩める (bare re-raise guard)、`AssessmentError` を素通し再 raise |
| `app/analysis/classifier/gemini.py` | `response_schema` を dict 直接渡し、`parse_assessment` 経由、`_translate_error` を新 ACL 規約 (`AIProvider*Error` 9 種への翻訳) に整合 |
| `app/analysis/classifier/deepseek.py` | 同上 (`gemini.py` と同パターン、OpenAI 互換 SDK 例外を翻訳) |
| `app/analysis/assessment/service.py` | `execute()` を ACL boundary 化 (`AIProviderError` catch + `map_provider_to_assessment` で詰め替え)、`_handle_in_scope` / `_handle_out_of_scope` で同 tx audit append |
| `app/analysis/tasks.py::assess_content` | 3 except dispatch (`AssessmentRecoverableError` / `AssessmentTerminalSkipError` / `Exception`)。inline retry は本 spec では持たず、cron 救済で統一 (logfire 設計で別途検討) |
| `app/observability/categories.py` | `Layer1Category.NON_RETRYABLE_KEEP_EXTRACTION` 追加、`Stage.CLASSIFICATION` → `Stage.ASSESSMENT` rename |
| `app/observability/domain/payloads.py` | `AssessmentPayload` 追加 (raw_category / raw_topic / category_id 等の field を持つ flat 型) |
| `alembic/versions/XXXX_assessment_audit.py` | `pipeline_events.stage` CHECK 制約 / `payload.kind` 値 / `pipeline_events.category` CHECK 制約を一括 migration |

### Stage 3 互換のため触らないファイル

| パス | 理由 |
|---|---|
| `app/analysis/extraction/**` | Stage 3 のコード一切 touch しない (原則 4) |
| `app/analysis/errors/provider.py` | provider 側に Stage 別 hint を入れない方針 (原則 4)。9 種の class / `CODE` / `INLINE_RETRY` / foundation marker 継承は既存のまま維持。Stage 4 ACL は Stage 4 側の tuple で表現 |
| `app/analysis/errors/__init__.py` | provider 側 re-export を変更しない |
| `app/analysis/errors/extraction.py` | Stage 3 specific 例外、本 PR 対象外 |
| `app/observability/categories.py` の foundation marker (`RetryableError` / `NonRetryableKeepArticle` / `NonRetryableDropArticle`) | Stage 3 互換のため維持。Stage 4 は継承しない |

---

## 関連 PR

(本 spec が merge され、PR 分割方針が確定したら追記)

---

## 関連仕様

- `specs/pipeline-events-error-taxonomy.md` — foundation (Layer 1 marker / DB schema / AuditRepository パターン)
- `specs/pipeline-events-stage3-extraction.md` — Stage 3 (extraction) の確定仕様 (Stage 4 はこの構造を Stage 4 文脈で起こし直す)
- `specs/stage4-assessment-rename.md` — Stage 4 命名統一 rename 系 PR 群 (PR3.5-d.0/d.1/d.2/d.3、本 spec の前提条件)
- `docs/observability/pipeline-events-design.md` — pipeline_events 監査基盤 ADR (Status: Accepted)
