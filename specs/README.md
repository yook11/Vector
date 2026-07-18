# Vector Implementation Specifications

このディレクトリには、Vectorの公開可能な実装仕様書だけを配置する。
目的は、実装前に定義した問題・制約・完了条件と、実装後の検証結果を追跡できる状態にすることにある。

## Scope

含めるもの:

- 実装対象と責務境界が明確な機能仕様・リファクタ仕様
- 実装が守る不変条件と、停止条件または検証条件を持つ文書
- production code、test、PR、commitのいずれかへ追跡できる文書

含めないもの:

- ADR、開発履歴、対話ログ、作業メモ
- 調査・リサーチの生資料、監査レポート、脆弱性の再現手順
- roadmap、アイデア、合意前の議論
- 個人用ワークフローやポートフォリオ文書

非公開資料はローカルの`private-notes/`で管理し、Gitでは追跡しない。公開仕様から非公開資料を参照しない。

## Status

- `Draft`: 検討中で、実装契約として未確定
- `Accepted`: 実装可能な状態まで確定
- `Partially implemented`: 一部を実装済みで、残作業が文書内で特定されている
- `Implemented`: 実装と検証が完了

文書冒頭の`Status`を状態の正本とする。本文中のチェックリストは、実装時の受け入れ条件として保持する。

## Required Sections

新規または更新する仕様書は、原則として次の情報を持つ。

- `Problem`: 解く問題
- `Evidence`: 関連するschema、test、設定、既存実装、一次情報
- `Invariants`: 継続して守る制約と境界条件
- `Non-goals`: 今回扱わないこと
- `Done`: 達成状態と停止条件
- `Implementation`: 対応するPR、commit、production code
- `Verification`: 対応するtestと検証結果

API契約の正本はFastAPIのPydantic schema、DB変更の正本はAlembic migration、環境変数の正本は設定層とする。仕様とこれらが食い違う場合は、相違を放置せず仕様を更新する。

## Directory Map

| Directory | Responsibility |
|---|---|
| `admin/` | 管理機能 |
| `agent/` | Q&A agent、planning、内部検索 |
| `analysis/` | AI分析工程 |
| `audit/` | `backend/app/audit`と監査イベントの実装契約 |
| `collection/` | 記事取得・補完 |
| `insights/` | briefingなどの集約結果 |
| `news/` | ニュース表示契約 |
| `observability/` | Logfireと処理結果メトリクス |
| `pipeline/` | pipeline横断の型・命名・永続化契約 |
| `platform/` | 複数bounded contextに関係する基盤変更 |

`audit/`には監査機能の実装仕様だけを置く。コードレビューやセキュリティ監査の結果は置かない。

## Current Specifications

| Area | Specification | Status |
|---|---|---|
| Admin | [ニュースソース状態確認](./admin/admin-source-health.md) | Implemented |
| Analysis | [Assessment Category Taxonomy](./analysis/assessment-category-taxonomy.md) | Implemented |
| Analysis | [InScopeAnalyzedArticle](./analysis/in-scope-analyzed-article.md) | Implemented |
| Agent | [Internal query cap / planner draft audit](./agent/internal-query-cap-and-planner-draft-audit.md) | Implemented |
| Agent | [Internal Retrieval Article Search](./agent/internal-retrieval-article-search.md) | Implemented |
| Agent | [Internal Retrieval Query Embedding](./agent/internal-retrieval-query-embedding.md) | Implemented |
| Agent | [Internal Retrieval Structure](./agent/internal-retrieval-structure.md) | Implemented |
| Agent | [QuestionPlan variant types](./agent/question-plan-variant-types.md) | Implemented |
| Agent | [Question Planner Audit](./agent/question-planner-audit.md) | Implemented |
| Agent | [Question Planner / Routing](./agent/question-planner-routing.md) | Implemented |
| Analysis | [Stage 4 Assessment naming](./analysis/stage4-assessment-rename.md) | Implemented |
| Audit | [Audit skip escape policy](./audit/audit-skip-escape-policy.md) | Partially implemented |
| Audit | [Audit scope / dispatch / backfill](./audit/pipeline-events-audit-scope-dispatch-backfill.md) | Implemented |
| Audit | [Audit stage SSoT](./audit/pipeline-events-audit-stage-ssot.md) | Implemented |
| Audit | [Failure attribute projection](./audit/pipeline-events-failure-attribute-projection.md) | Implemented |
| Collection | [Acquisition failure types](./collection/article-collection-acquisition-failure-types.md) | Implemented |
| Collection | [External fetch error mapping](./collection/article-collection-external-fetch-error-mapping.md) | Implemented |
| Collection | [Source Completion Profile](./collection/source-completion-profile.md) | Implemented |
| Collection | [Stage 1 acquisition vocabulary](./collection/stage1-acquisition-vocabulary-unification.md) | Implemented / authority |
| Collection | [Stage 2 title extraction](./collection/stage2-title-extraction.md) | Implemented |
| Insights | [Briefing schema naming](./insights/briefing-schema-naming.md) | Implemented |
| News | [Article card key points](./news/article-card-key-points.md) | Implemented |
| News | [CategoryBrief rename](./news/category-brief-rename.md) | Accepted |
| Observability | [Assessment outcome metrics](./observability/logfire-assessment-outcome-metrics.md) | Implemented |
| Observability | [Completion outcome metrics](./observability/logfire-completion-outcome-metrics.md) | Implemented |
| Observability | [Curation outcome metrics](./observability/logfire-curation-outcome-metrics.md) | Implemented |
| Observability | [Embedding outcome metrics](./observability/logfire-embedding-outcome-metrics.md) | Implemented |
| Pipeline | [Analyzable article naming](./pipeline/analyzable-articles-naming.md) | Implemented |
| Pipeline | [Analyzed article naming](./pipeline/analyzed-articles-naming.md) | Implemented |
| Pipeline | [Observed article payload naming](./pipeline/observed-article-payload-naming.md) | Implemented |
| Pipeline | [Tier 1 article ID naming](./pipeline/tier1-analyzable-article-id-naming.md) | Implemented |
| Pipeline | [Tier 2 embedding dimension SSoT](./pipeline/tier2-embedding-dimension-ssot.md) | Implemented |
| Pipeline | [Tier 3 curation / completion naming](./pipeline/tier3-curation-completion-naming.md) | Implemented |
| Pipeline | [Typed pipeline preconditions](./pipeline/typed-pipeline-preconditions.md) | Partially implemented |
| Platform | [Value object BC migration](./platform/vo-bc-migration-plan.md) | Implemented |

## Maintenance Rules

- 実装と同じ変更で`Status`、実装リンク、検証結果を更新する。
- `Implemented`へ変更する前に、対応するproduction codeとtestを確認する。
- 公開仕様から`private-notes/`、ignored file、ローカル端末のパスを参照しない。
- 調査結果は検証済みの要点だけを`Evidence`へ取り込む。
- Superseded、Rejected、Abandonedになった文書は公開仕様から外し、履歴資料として整備しない。
