# InScopeAnalyzedArticle Spec

Status: Draft
Created: 2026-06-29
Scope: in-scope analyzed article snapshot を永続化前・読み戻し時に同じ domain 型で保証する

## Problem

現在の Stage 4 in-scope 保存では、`AssessmentRepository.save_in_scope()` が `ReadyForAssessment` と `InScope` を直接合成して `analyzed_articles` に INSERT している。

`ReadyForAssessment` は assessment を実行できる入力であることを保証し、`InScope` は assessment 結果が対象範囲内であることを保証する。しかし、`analyzed_articles` に保存してよい完成 snapshot を表す型がない。

内部検索では DB から `analyzed_articles` を読み戻し、agent の根拠として使う。保存前と読み戻し時で同じ不変条件を通すため、`ReadyForAssessment + InScope` の合成結果を `InScopeAnalyzedArticle` として定義する。

## Evidence

- `Signal.title_ja` / `Signal.summary_ja` は Stage 3 curation domain で正規化・非空を保証している。
- `ReadyForAssessment.translated_title` / `ReadyForAssessment.summary` は `Field(min_length=1)` で assessment 入力として非空を保証している。
- `InScope` は `InScopeCategory` により `OUT_OF_SCOPE` を型レベルで排除し、`investor_take` と `key_points` を domain 型として保証している。
- `InScopeCategory` は in-scope category 12 値の SSoT であり、AI schema 用の全 category 値は `assessment-category-taxonomy.md` で定義する `assessment_category_values()` が合成する。
- `AssessmentRepository.save_in_scope()` は現在、`ready.translated_title`, `ready.summary`, `in_scope.investor_take`, `in_scope.key_points` を直接 `AnalyzedArticleRecord` に保存している。
- `AnalyzedArticleRecord` には `translated_title != ''`, `summary != ''`, `investor_take != ''` の DB check constraint がある。
- collection domain の `AnalyzableArticle.published_at` は必須だが、現行 DB / public API は `published_at` を nullable として扱っている。この整合性を締める `published_at NOT NULL` 化は別作業とする。
- 現在、保存可能な in-scope analyzed article snapshot を表す domain 型は存在しない。

## Decision

`InScopeAnalyzedArticle` を追加する。

これは DB row の ORM model ではなく、analysis domain の保存可能 in-scope analyzed article snapshot である。

実装場所は `backend/app/analysis/analyzed_article.py` とする。

`InScopeAnalyzedArticle` は assessment で生成されるが、用途は assessment に閉じない。保存済み analyzed article として、assessment の保存経路、embedding / internal retrieval の読み戻し経路、agent / user に見せる分析済み記事が共有する境界型である。そのため `backend/app/analysis/assessment/` 配下ではなく、`backend/app/analysis/` 直下に置く。

## Type

```python
class InScopeAnalyzedArticle(BaseModel):
    model_config = ConfigDict(frozen=True)

    curation_id: int = Field(gt=0)
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    assessment_result: InScope

    @classmethod
    def from_ready_and_assessment_result(
        cls,
        *,
        ready: ReadyForAssessment,
        assessment_result: InScope,
    ) -> Self:
        ...

    @classmethod
    def from_persisted_values(
        cls,
        *,
        curation_id: int,
        translated_title: str,
        summary: str,
        category_slug: str,
        investor_take: str,
        key_points: object,
    ) -> Self:
        ...
```

Field の意味:

- `curation_id`: 元になった `ArticleCuration.id`。
- `title`: Stage 3 curation で生成され、`ReadyForAssessment.translated_title` / `AnalyzedArticleRecord.translated_title` に保存される日本語タイトル。
- `summary`: Stage 3 curation で生成され、`ReadyForAssessment` が保持していた日本語要約。
- `assessment_result`: Stage 4 assessment の対象範囲内結果。既存 `InScope` domain 型をそのまま使う。

`InScopeAnalyzedArticle` は `title` / `summary` を `InScope` に混ぜ込まない。`InScope` は assessment result、`InScopeAnalyzedArticle` は保存可能 snapshot という責務を分ける。

`assessment_result` の型は `InScope` である。`category` だけを field として持つのではなく、`investor_take` / `key_points` / `mentions` を含む assessment result 全体を保持する。`InScopeAnalyzedArticle` という外側の型名が in-scope であることを示すため、field 名は `in_scope_assessment` ではなく `assessment_result` とする。

`model_config = ConfigDict(frozen=True)` により、生成後の `title` / `summary` / `assessment_result` の差し替えを防ぐ。ただし `frozen=True` は「正規 factory を必ず通った」ことまでは保証しない。その経路保証は repository / service が factory を使うことを test で固定して守る。

`InScopeAnalyzedArticle` は `published_at` を持たない。`published_at` は元記事 `AnalyzableArticleRecord` 側の事実であり、保存可能 snapshot の不変条件には混ぜない。内部検索や回答生成で必要な場合は、検索 hit / projection 側に持たせる。

## Write Path

保存時は repository が `ReadyForAssessment` と `AssessmentCall[InScope]` を直接分解しない。

`AssessmentService` は in-scope branch で `InScopeAnalyzedArticle.from_ready_and_assessment_result()` を呼び、保存可能 snapshot を構築する。

```python
article = InScopeAnalyzedArticle.from_ready_and_assessment_result(
    ready=ready,
    assessment_result=call.result,
)
```

`AssessmentRepository.save_in_scope()` は `InScopeAnalyzedArticle` を受け取る。

```python
async def save_in_scope(
    self,
    article: InScopeAnalyzedArticle,
) -> int | None:
    ...
```

保存時の DB values は `article` から作る。

```python
values(
    curation_id=article.curation_id,
    translated_title=article.title,
    summary=article.summary,
    category_id=category_id,
    investor_take=article.assessment_result.investor_take,
    key_points=[k.model_dump() for k in article.assessment_result.key_points],
)
```

`category_id` 解決は引き続き repository の責務とする。`InScopeAnalyzedArticle` は DB category id を持たず、`article.assessment_result.category` として domain の `InScopeCategory` を保持する。

## Read Path

DB から `analyzed_articles` を読み戻す時も、`InScopeAnalyzedArticle` を復元する。

読み戻し repository は次の DB 値を `InScopeAnalyzedArticle.from_persisted_values()` に渡す。この factory が `InScope` を再構築し、保存済み snapshot として再検証する。

- `AnalyzedArticleRecord.curation_id`
- `AnalyzedArticleRecord.translated_title`
- `AnalyzedArticleRecord.summary`
- `Category.slug`
- `AnalyzedArticleRecord.investor_take`
- `AnalyzedArticleRecord.key_points`

復元イメージ:

```python
article = InScopeAnalyzedArticle.from_persisted_values(
    curation_id=row.curation_id,
    translated_title=row.translated_title,
    summary=row.summary,
    category_slug=row.category_slug,
    investor_take=row.investor_take,
    key_points=row.key_points,
)
```

`from_persisted_values()` は `key_points is None` を旧行互換として `[]` に正規化する。`key_points` が list だが `KeyPoint` として不正な場合、`InScope` validation が失敗するため data invariant breach として扱う。

## Internal Retrieval Integration

内部検索では、検索対象 article row を `InScopeAnalyzedArticle` に復元してから agent 用 projection へ詰め替える。

内部検索で返してよい記事は次を満たす。

```text
InScopeAnalyzedArticle として復元できる
かつ
AnalyzedArticleRecord.embedding IS NOT NULL
```

`embedding IS NOT NULL` は「内部ベクトル検索の対象になれる」条件であり、記事内容そのものではない。そのため `InScopeAnalyzedArticle` には含めず、repository の query 条件として保証する。embedding vector 自体は返却型に持たせない。

`published_at` は現行 DB / API が nullable のため `datetime | None` とする。`published_at NOT NULL` 化を別作業で行った後に、この projection 側も `datetime` へ締める。

回答生成に渡す `InternalArticleContent` は `InScopeAnalyzedArticle` から projection する。

```python
class InternalArticleContent(BaseModel):
    title: str
    summary: str
    key_points: list[str]
    mentions: list[str]
    published_at: datetime | None
```

`key_points` は `article.assessment_result.key_points[].content` から作る。`mentions` は `article.assessment_result.key_points[].mentions[].surface` だけを使う。

## Invariants

`InScopeAnalyzedArticle` は次を保証する。

- `curation_id` は正の整数である。
- `title` は非空である。
- `summary` は非空である。
- `assessment_result` は既存 `InScope` domain 型である。
- `assessment_result.category` は `OUT_OF_SCOPE` を取りえない。
- `assessment_result.investor_take` は非空である。
- `assessment_result.key_points` は既存 `KeyPoint` domain 型の list である。
- 生成後に field を差し替えられない frozen snapshot である。

Write path の追加不変条件:

- `analyzed_articles` への INSERT は `InScopeAnalyzedArticle` からだけ行う。
- `ReadyForAssessment + InScope` の合成は repository ではなく `from_ready_and_assessment_result()` で行う。

Read path の追加不変条件:

- `analyzed_articles` から agent / internal retrieval に渡す前に `from_persisted_values()` で `InScopeAnalyzedArticle` へ復元する。
- 復元できない row は domain invariant breach として扱う。
- `key_points NULL` の旧行は `[]` として復元する。

## Non-goals

- DB schema は変更しない。
- `published_at NOT NULL` 化はこの作業では行わない。DB constraint / ORM nullability / API schema の整合は別作業で扱う。
- `OutOfScopeAnalyzedArticle` は作らない。
- `UserVisibleArticle` という別名の専用型は作らない。現時点の visible analyzed article boundary は `InScopeAnalyzedArticle` とする。
- public API schema は変更しない。
- `ArticleBrief` / `ArticleDetail` の生成経路はこの作業では変更しない。
- `InScope` に title / summary を追加しない。
- category catalog の DB 事前検証は既存 `assert_category_catalog_covers_enum()` を維持し、対象は `InScopeCategory` のみとする。
- `AssessmentCategory = InScopeCategory | OutOfScopeCategory` はこの仕様では追加しない。

## Test Plan

- `InScopeAnalyzedArticle.from_ready_and_assessment_result()` が `ReadyForAssessment` と `InScope` から snapshot を構築する。
- `title` が空なら validation error になる。
- `summary` が空なら validation error になる。
- `curation_id <= 0` は validation error になる。
- `assessment_result.category` に `OUT_OF_SCOPE` は入れられない。
- 生成後に `title` / `summary` / `assessment_result` を差し替えると validation error になる。
- repository `save_in_scope()` が `InScopeAnalyzedArticle` から `AnalyzedArticleRecord` を保存する。
- `save_in_scope()` は category slug から category id を解決する既存挙動を維持する。
- `InScopeAnalyzedArticle.from_persisted_values()` が DB row 相当の値から snapshot を復元できる。
- `key_points NULL` は `[]` として復元される。
- 不正な `key_points` は validation error になる。
- internal retrieval repository は検索 hit を構築する前に `InScopeAnalyzedArticle` を復元する。
- internal retrieval repository は `embedding IS NOT NULL` の row だけを検索対象にする。
- internal retrieval の返却型は embedding vector / raw DB row / raw key_points JSON を含まない。

## Done

- 保存前に `ReadyForAssessment + InScope` の合成結果を domain 型で保証する方針が明文化されている。
- DB から読み戻す時も同じ `InScopeAnalyzedArticle` を使って保証する方針が明文化されている。
- write path は `from_ready_and_assessment_result()`、read path は `from_persisted_values()` を使う方針が明文化されている。
- `InScope` はそのまま再利用し、title / summary は保存可能 snapshot 側に保持する方針が明文化されている。
- `published_at NOT NULL` 化は別作業に分離する方針が明文化されている。
- 内部検索が `InScopeAnalyzedArticle` を経由して agent 用 content を作る方針が明文化されている。
