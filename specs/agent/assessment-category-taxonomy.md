# Assessment Category Taxonomy Spec

Status: Draft
Created: 2026-06-30
Scope: Stage 4 assessment category enum の重複解消と AI schema 用 category values

## Problem

現在の Stage 4 assessment domain は、`InScopeCategory` と `ValidCategory` の 2 つの enum に in-scope category 12 値を重複定義している。

`InScopeCategory` は `InScope.category` の型であり、`OUT_OF_SCOPE` を型レベルで排除する。一方 `ValidCategory` は AI 境界に提示する全 category 値として、同じ 12 値に `out_of_scope` を加えた 13 値を持つ。

この重複は category 追加時の更新漏れを生む。`InScope.category` の型安全性を保ったまま、in-scope category の SSoT を 1 つに寄せる。

## Evidence

- `InScopeCategory` は `InScope.category` に使われ、`out_of_scope` を値として持たない。
- `ValidCategory` は `parse_assessment()` と AI schema の enum values で使われている。
- `AssessmentRepository.missing_category_slugs()` は `InScopeCategory` を DB category catalog と照合している。
- 現在の drift test は `InScopeCategory` と `ValidCategory - OUT_OF_SCOPE` の一致を検証している。
- Python enum は既存 enum を継承して値を追加できない。
- `AssessmentCategory = InScopeCategory | OutOfScopeCategory` の union alias は iterable でも constructible でもないため、schema enum values や parse の主処理には直接使えない。

## Decisions

- `InScopeCategory` を in-scope category 12 値の SSoT にする。
- `OutOfScopeCategory` を 1 値 enum として追加する。
- `ValidCategory` は廃止する。
- `AssessmentCategory = InScopeCategory | OutOfScopeCategory` は今は追加しない。
- AI schema 用の全 category 値は `assessment_category_values()` で合成する。
- `OutOfScope` に category field は追加しない。`OutOfScope` 型そのものが out-of-scope 判定を表す。
- `InScope.category` は引き続き `InScopeCategory` のままとし、`OUT_OF_SCOPE` を型レベルで保持できない状態を維持する。

## Types

```python
class InScopeCategory(StrEnum):
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


class OutOfScopeCategory(StrEnum):
    OUT_OF_SCOPE = "out_of_scope"
```

`AssessmentCategory` union alias は、実際に「in-scope / out-of-scope のどちらでもよい category 値」を保持する field が必要になった時点で追加する。現時点では、parse と schema は具体 enum と helper で足りるため導入しない。

## Helper

```python
def assessment_category_values() -> tuple[str, ...]:
    return tuple(category.value for category in InScopeCategory) + (
        OutOfScopeCategory.OUT_OF_SCOPE.value,
    )
```

この helper は AI schema enum values の SSoT である。

利用箇所:

- `ASSESSMENT_TOOL_SCHEMA["properties"]["category"]["enum"]`
- `ASSESSMENT_GEMINI_SCHEMA["properties"]["category"]["enum"]`

順序は既存 schema と同じく in-scope category の定義順、最後に `out_of_scope` とする。

## Parse Behavior

`parse_assessment()` は `ValidCategory(category_raw)` を使わない。

新しい dispatch:

```python
if category_raw == OutOfScopeCategory.OUT_OF_SCOPE.value:
    return OutOfScope(
        investor_take=investor_take_raw,
        key_points=key_points,
    )

try:
    category = InScopeCategory(category_raw)
except ValueError as exc:
    raise AssessmentResponseInvalidError(
        AssessmentResponseDefect.CATEGORY_UNKNOWN_VALUE
    ) from exc

return InScope(
    category=category,
    investor_take=investor_take_raw,
    key_points=key_points,
)
```

`out_of_scope` は `InScopeCategory(category_raw)` より先に分岐する。そうしないと `out_of_scope` が unknown category として誤分類される。

未知 category 文字列はこれまで通り `AssessmentResponseDefect.CATEGORY_UNKNOWN_VALUE` に分類する。`ValueError` は `__cause__` として保持する。

## Impact Scope

変更対象:

- `backend/app/analysis/assessment/domain/result.py`
  - `ValidCategory` を削除する。
  - `OutOfScopeCategory` を追加する。
  - `assessment_category_values()` を追加する。
- `backend/app/analysis/assessment/ai/parse.py`
  - `ValidCategory` 参照を削除する。
  - `OutOfScopeCategory` と `InScopeCategory` で dispatch する。
- `backend/app/analysis/assessment/ai/schema_tool.py`
  - `[c.value for c in ValidCategory]` を `assessment_category_values()` に置き換える。
- `backend/tests/analysis/assessment/domain/test_result.py`
  - `ValidCategory` との一致テストを削除する。
  - `assessment_category_values()` の完全性テストへ置き換える。
- `backend/tests/analysis/assessment/ai/test_parse_assessment.py`
  - 既存 semantics が維持されることを確認する。

変更しない対象:

- DB schema。
- public API schema。
- `InScope.category` の型。
- `OutOfScope` の shape。
- category catalog coverage。DB category catalog と照合する対象は引き続き `InScopeCategory` のみ。

## Invariants

- `InScope.category` は `OutOfScopeCategory.OUT_OF_SCOPE` を持てない。
- AI schema は in-scope 12 値 + `out_of_scope` の 13 値を公開する。
- `assessment_category_values()` は重複値を返さない。
- `out_of_scope` は `OutOfScope` に dispatch される。
- in-scope category slug は `InScope` に dispatch される。
- unknown category は `CATEGORY_UNKNOWN_VALUE` になる。
- category catalog coverage は out-of-scope を DB category として要求しない。

## Non-goals

- `AssessmentCategory = InScopeCategory | OutOfScopeCategory` は今回追加しない。
- `OutOfScope` に category field は追加しない。
- `InScope.category` を 13 値 enum + validator に変更しない。
- DB category table に `out_of_scope` を追加しない。
- AI response payload の wire shape は変更しない。

## Test Plan

- `InScopeCategory` が 12 値を持つ。
- `InScopeCategory("out_of_scope")` が失敗する。
- `OutOfScopeCategory.OUT_OF_SCOPE.value == "out_of_scope"`。
- `assessment_category_values()` が `InScopeCategory` 全値を定義順で含む。
- `assessment_category_values()` の末尾が `"out_of_scope"`。
- `assessment_category_values()` に重複がない。
- DeepSeek / Gemini schema の category enum が `assessment_category_values()` と一致する。
- `parse_assessment()` が `out_of_scope` を `OutOfScope` にする。
- `parse_assessment()` が各 `InScopeCategory` 値を `InScope` にする。
- `parse_assessment()` が unknown category を `CATEGORY_UNKNOWN_VALUE` にする。
- category catalog coverage は `InScopeCategory` のみを検査する。

## Done

- in-scope category 12 値の重複定義がなくなる方針が明文化されている。
- `InScope.category` の型レベル out-of-scope 排除を維持する方針が明文化されている。
- AI schema 用 category values の SSoT が `assessment_category_values()` であることが明文化されている。
- `AssessmentCategory` union alias を現時点では追加しない理由が明文化されている。
