# Architecture Decision Records (ADR)

Vector の重要なアーキテクチャ決定とその根拠を記録する。各 ADR は
**何を決めたか**だけでなく、**なぜそうしたか（検討した代替・トレードオフ・
受容したリスク）** を残すことを目的とする。

> 形式: 連番 `NNN_topic.md`。ステータスは `Proposed` → `Accepted` → 必要に応じ
> `Superseded`。新しい決定が古い決定を置き換える場合は両方に相互リンクを張る。

## Index

| # | 決定 | ステータス | 決め手 / トレードオフ |
|---|------|-----------|----------------------|
| [001](001_taskiq_over_arq.md) | タスクキューに **taskiq** を採用（arq を不採用） | Accepted | arq が maintenance-only に対し taskiq は活発・自動リトライ・高速。代償の「cron が別プロセス」はコンテナ分離で許容 |
| [002](002_auth_schema_separation.md) | PostgreSQL を **auth / public スキーマ分離** | Accepted | Better Auth CLI と Alembic の migration 競合を、別 DB（運用コスト過大）でなく同一 DB の論理分離で解決 |
| [003](003_bff_proxy_pattern.md) | **BFF プロキシパターン**による認証 | Accepted | Next.js を唯一の公開エントリとしセッション検証→信頼ヘッダ注入。backend のステートレス維持・シークレット保護・多層防御。代償は 1 ホップ（<1ms）と proxy 障害の単一障害点 |
| [004](004_unit_of_work_service_convention.md) | Service レイヤーの **Unit of Work 規約** | Accepted | 既存エンティティの状態変更に `repo.save()` を呼ばない（SQLAlchemy UoW 依存）。Rich Model を汚さない。代償は「どこで DB 変更が起きるか」の読みにくさ→ADR + docstring で補う |
| [005](005_rsc_test_strategy.md) | **RSC (Server Component) のユニットテスト戦略** | Proposed | E2E 間接カバーのみだった RSC ロジックに node project の専用検証経路を新設し test pyramid の欠落を埋める |
| [006](006_better_auth_rate_limit_strategy.md) | Frontend の **rate limit / cookieCache 戦略** | Accepted (§1/§4 は [009](009_proxy_rate_limit_multitier.md) が supersede) | Red-team で発見した anon→1 user 再現の Critical chain (C8) への対応。proxy.ts の IP limiter は Redis + fail-open のまま正しい |
| [007](007_auth_ratelimit_db_storage.md) | Better Auth ログイン limiter を **DB-backed 化** + redis-rl eviction 修正 | Accepted | Redis 障害時の fail-open 穴（OWASP API2）を構造的に除去。ただし atomic でないため「best-effort limiter」と保証範囲を明示（過大表現しない） |
| [008](008_pipeline_events_audit.md) | **pipeline_events** パイプライン監査基盤 | Accepted | 非同期 11 Stage の「黙って消える失敗」を append-only イベントログで可視化。過積載 enum を直交軸へ投影（「アラートが事実と矛盾して黙る」を回避）／成功=同tx・失敗=別tx／監査=事実の witness |
| [009](009_proxy_rate_limit_multitier.md) | proxy rate limit を **request-class × identity の multi-tier** 化 | Accepted | 通常閲覧の誤 429 (prefetch fan-out が単一 IP bucket を消費) を解消。`_rsc` は寛容 ceiling で別財布 (全 skip は C8 再オープン)／session+IP の two-tier-AND で偽造 cookie バイパスを封鎖／未解決 IP は identity でなく異常 (read=fail-open) |

## 移行記録（narrative rationale）

連番 ADR とは別に、大きな技術基盤の乗り換え理由を記述したドキュメント。
決定の「型」より背景の説明に重きを置く。

| ドキュメント | 内容 |
|------------|------|
| [SQLModel → SQLAlchemy DeclarativeBase 移行](sqlmodel-to-declarative-migration.md) | `sa_column` への逃げが常態化し 2 つの書き方が混在する SQLModel の制約を解消 |
| [値オブジェクト + SQLAlchemy Declarative 移行](value_objects_sqlalchemy_migration.md) | SQLModel では VO を ORM 層に統合できず ~90 行/VO のボイラープレートが必要だった制約を解消 |

## 関連ドキュメント

- [docs/observability/pipeline-events-design.md](../observability/pipeline-events-design.md) — pipeline_events 監査基盤の設計（全 stage の成功/失敗/AI raw response を SQL 再構成）
- [docs/observability/pipeline-events-failure-attributes.md](../observability/pipeline-events-failure-attributes.md) — 失敗分類の attribute 設計
- [docs/prompt_design.md](../prompt_design.md) — LLM プロンプト設計
