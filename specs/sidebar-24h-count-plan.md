# サイドバー記事件数を「直近24時間」表示に変更

## 目的 / 背景

サイドバーのカテゴリ/トピック件数を、現状の **総件数** から **直近24時間にトピック分類された件数** に変更する。

### 問題意識

- 記事が積み上がると `9999+` のような表示になり、情報として死ぬ
- 本プロダクトの価値は時事性。サイドバーに置くべきは在庫量ではなく **「今動いているか」のシグナル**
- 総件数は数が大きくなるほど「カテゴリ間の差」を表現できなくなる

### 採択した方針

- 集計クエリに 24h の時間条件を追加（自然にスライドする）
- 基準時刻は **`article_analyses.analyzed_at`**（AI が分類を完了した時刻）
- API フィールド名は **`recentCount`**（意味で命名、24h はドキュメント定義）
- 0件時は数値を非表示（カテゴリ/トピック名のみ表示）

### 基準時刻として `analyzed_at` を採る理由

このアプリのカテゴリ/トピックは **AI 分析の産物**。バケツへの所属は分類が完了した瞬間に発生する。
よってバケツへの所属を時系列で測る軸は、記事の `published_at`（媒体側の属性）ではなく、
分類イベントの時刻 `analyzed_at` が概念的に正しい。

副次的効果:

- ユーザーが「サイドバーに新着が見える」と感じるタイミングと一致する（= 分類された瞬間）
- `published_at` の NULL/再投稿による日時揺れの影響を受けない（`analyzed_at` は NOT NULL のシステム時刻）
- JOIN が `Topic ← article_analyses` の1段で済む

「記事の鮮度（`published_at` 順）」は記事一覧ページの責務として分離し、サイドバーの集計ロジックには持ち込まない。

---

## スコープ

- `GET /api/v1/categories` のレスポンススキーマ変更（`articleCount` 削除 → `recentCount` 追加）
- バックエンド集計クエリに 24h 条件追加
- インデックス追加（Alembic マイグレーション）
- フロントの 0件時非表示対応
- TypeScript 型の再生成
- 関連テストの更新

---

## 設計方針

### Repository クエリ変更

`backend/app/repositories/category.py` の 2 クエリを更新する。SQLAlchemy 2.0 / クエリビルダーで構築する。
カットオフは Python 側で算出して bind パラメータとして渡す（文字列 SQL を組み立てない）。

```python
from datetime import UTC, datetime, timedelta

cutoff = datetime.now(UTC) - timedelta(hours=24)

select(
    Topic.category_id,
    func.count(ArticleAnalysis.id).label("recent_count"),
)
.join(ArticleAnalysis, ArticleAnalysis.topic_id == Topic.id)
.where(ArticleAnalysis.analyzed_at > cutoff)
.group_by(Topic.category_id)
```

トピック単位の `fetch_topic_stats()` も同様に `analyzed_at > cutoff` を追加し、`group_by(Topic.id)` 構造を維持する。

### インデックス追加（Alembic マイグレーション必須）

現状 `article_analyses` には `topic_id` 単独 index しかない。
24h フィルタ + `topic_id` での GROUP BY を効率化するため、複合インデックスを追加する:

```python
op.create_index(
    "ix_article_analyses_topic_id_analyzed_at",
    "article_analyses",
    ["topic_id", "analyzed_at"],
)
```

既存の `topic_id` 単独 index は **当面残す**（他のクエリで使われる可能性、複合 index への移行リスク回避）。
不要と判断できた段階で別 spec で削除を検討する。

### API スキーマ変更

`articleCount` を削除し `recentCount` を追加（破壊的変更）。
本 API の consumer はサイドバー1箇所のみであることを事前調査で確認済み。

```python
# backend/app/schemas/category.py
class CategoryDetail(SQLModel):
    # ...既存フィールド
    recent_count: int = 0  # 直近24時間に分類された記事数

# backend/app/schemas/embeds.py
class TopicStatEmbed(SQLModel):
    # ...既存フィールド
    recent_count: int = 0
```

OpenAPI description / docstring に「直近24時間に AI 分類が完了した記事の件数」と明記する。
24h という具体値はバックエンド内の定数として持つ（命名: `SIDEBAR_RECENT_WINDOW`）。

### フロント表示

`CategorySidebar.tsx` の 2 箇所で条件レンダリングに変更:

```jsx
{cat.recentCount > 0 && (
  <span className="ml-2 text-xs tabular-nums text-neutral-400 dark:text-neutral-600">
    {cat.recentCount}
  </span>
)}
```

`flex justify-between` のコンテナは件数 span がなくなった場合の見た目を実機で確認する。
カテゴリ名は左寄せで完結するため、右端要素消失でレイアウト破綻はないはずだが、要検証。

---

## 実装タスク

### Backend

1. `backend/app/schemas/embeds.py:24` — `article_count` → `recent_count`、docstring を「直近24時間に AI 分類が完了した記事の件数」に更新
2. `backend/app/schemas/category.py:11` — 同上
3. `backend/app/repositories/category.py`
   - 定数 `SIDEBAR_RECENT_WINDOW = timedelta(hours=24)` を定義
   - `fetch_topic_stats()` / `fetch_category_article_counts()` の両方に `cutoff` 算出と `ArticleAnalysis.analyzed_at > cutoff` の WHERE を追加
   - SELECT のラベルを `recent_count` に変更
4. `backend/app/services/category.py:20, 29, 38` — `row.article_count` / `article_count=` を `row.recent_count` / `recent_count=` に更新
5. Alembic マイグレーション作成
   - 名前例: `<rev>_add_topic_id_analyzed_at_index_on_analyses.py`
   - `ix_article_analyses_topic_id_analyzed_at` 複合 index を追加
   - downgrade で index drop
6. Repository テスト追加（`backend/tests/repositories/test_category.py` がなければ新規）:
   - 24h 境界（cutoff のちょうど前後）
   - 24h 内に複数 topic がある場合の GROUP BY 集計
   - 24h 外の記事はカウントされないこと
7. `backend/tests/test_routers/test_categories.py:126` — assert を `["articleCount"]` → `["recentCount"]` に変更、テストフィクスチャで 24h 内/外の `analyzed_at` を作り分けて期待値を確認

### Frontend

8. `npm run generate-types`（または `/gen-types` スキル）で `frontend/src/types/generated.ts` を再生成
9. `frontend/src/components/layout/CategorySidebar.tsx:113` — `{cat.articleCount}` を条件レンダリングに置換
10. `frontend/src/components/layout/CategorySidebar.tsx:137` — トピック側も同様
11. モバイル（`MobileSidebar`）が同コンポーネントを内包していることを確認、独自ロジックがあれば追従

---

## 検証

### コマンド検証

backend/CLAUDE.md に従い、タスク完了前に必ず実行する:

```bash
uv run ruff check app/
uv run ruff format --check app/
uv run pytest tests/ -x -q
```

### マイグレーション検証

```bash
uv run alembic upgrade head    # 新 index が作成される
uv run alembic downgrade -1    # index が drop される
uv run alembic upgrade head    # 再度適用、冪等性を確認
```

### 機能検証

- 開発環境で `GET /api/v1/categories` を叩き、レスポンスに `recentCount` のみ存在し `articleCount` が消えていることを確認
- フロントでサイドバーを開き:
  - 24h 内に分類された記事があるカテゴリ/トピックは件数が表示される
  - 24h 内に分類された記事がないカテゴリ/トピックは名前のみ表示される
  - モバイルでも同じ挙動

### 性能検証

- 開発 DB で `EXPLAIN ANALYZE` を取得し、新 index `ix_article_analyses_topic_id_analyzed_at` が使われていることを確認
- サイドバー表示時の API レスポンスタイムを計測（目安: 200ms 以内）

### `/review` スキル実行

---

## リスク

| リスク | 影響 | 対処 |
|---|---|---|
| パイプラインが詰まると 24h 件数が見かけ上減少する | サイドバーが「死んでいる」ように見える | サイドバーは「Vector の処理活動」を表す指標と仕様で整理。パイプライン健全性は別の監視で見る |
| 大量の記事をバッチで再分類すると 24h 件数が一時的に急増する | サイドバーが急に派手になる | 現状そのような運用はなく、必要なら `analyzed_at` の更新方針を別途設計 |
| 既存の `articleCount` を参照する見落とし箇所 | フロントで type error / undefined 表示 | 事前調査で全箇所列挙済み（下記参照ファイル）。`/gen-types` 後の TypeScript コンパイルで検出可能 |
| 複合 index の追加で書き込みコストが増える | `article_analyses` への INSERT が僅かに遅くなる | 単一 index 1つの追加。書き込み頻度から見て無視可能な影響と想定 |

---

## 参照ファイル

### Backend

- `backend/app/routers/categories.py:20-25` — エンドポイント定義（変更なし）
- `backend/app/services/category.py:12-43` — 集計組み立て
- `backend/app/repositories/category.py:34, 54` — SQL 集計（変更箇所）
- `backend/app/schemas/category.py:11` — `CategoryDetail`
- `backend/app/schemas/embeds.py:24` — `TopicStatEmbed`
- `backend/app/models/article_analysis.py:71-77` — `analyzed_at` / `topic_id` 定義
- `backend/alembic/versions/` — 新規マイグレーション配置先
- `backend/tests/test_routers/test_categories.py:76-127` — 既存テスト（要修正）

### Frontend

- `frontend/src/components/layout/CategorySidebar.tsx:113, 137` — 件数表示
- `frontend/src/components/layout/MobileSidebar.tsx` — モバイル版ラッパー
- `frontend/src/lib/api-client.ts:151-154` — `getCategories`
- `frontend/src/types/generated.ts:353, 573` — 自動生成型（再生成対象）
