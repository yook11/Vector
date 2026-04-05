# エンドポイントレビュー

全13エンドポイントを 1 本ずつ読み込み、API として妥当か・実装に無駄や抜けがないかを検証する。発見した論点と対処方針をエンドポイントごとに記録する。

## 前提: リファクタ完了 (PR #26)

Router/Service/Repository の3層分離、`Annotated[Model, Query()]` への移行、VO 型のクエリパラメータ直接使用、internal ID の外部識別子排除は完了済み
(`refactor/schema-router-review`, PR #26, merged at 2026-04-05)。

本レビューはその上で「**使いやすさ・効率・堅牢性**」を1枚ずつ見直す作業。

## レビューの6観点

各エンドポイントを次の観点で確認する。

| # | 観点 | チェックすること |
|---|---|---|
| 1 | 目的・要件 | このエンドポイントはクライアントのどのユースケースを満たすために存在するか |
| 2 | API 設計 | URL・HTTP メソッド・ステータスコードは REST 的に妥当か。クライアントにとって使いやすいか |
| 3 | スキーマ設計 | リクエスト/レスポンスの形状は適切か。不要なフィールド・内部 ID の露出・ドメイン表現の歪みはないか |
| 4 | クエリ効率 | 必要なデータを最小コストで取得できているか。N+1・不要な JOIN・COUNT の重複実行はないか |
| 5 | ロジック効率 | 取得したデータからの組み立てに冗長がないか。Service 層の責務分担は適切か |
| 6 | 異常系・境界条件 | 404 / 422 / 409 の出し分け、空結果、ページ境界、権限チェック、重複リクエストなどが正しく扱われるか |

## レビュー順序

**「複雑度の高いコア機能 → 同じパターンの派生 → 周辺」** の順。コアで確立した設計判断を派生に波及させ、差分レビューで効率化する。

### Phase 1: 読み取りコア

| # | Method | Path | ファイル | 状態 |
|---|---|---|---|---|
| 1 | GET | `/api/v1/articles` | [articles-list.md](articles-list.md) | 未着手 |
| 2 | GET | `/api/v1/categories` | [categories-list.md](categories-list.md) | 未着手 |
| 3 | GET | `/api/v1/articles/{id}` | [articles-detail.md](articles-detail.md) | 未着手 |
| 4 | GET | `/api/v1/articles/{id}/similar` | [articles-similar.md](articles-similar.md) | 未着手 |

### Phase 2: ユーザー機能 (watchlist)

| # | Method | Path | ファイル | 状態 |
|---|---|---|---|---|
| 5 | GET | `/api/v1/me/watchlist` | [watchlist-list.md](watchlist-list.md) | 未着手 |
| 6 | POST | `/api/v1/me/watchlist` | [watchlist-add.md](watchlist-add.md) | 未着手 |
| 7 | DELETE | `/api/v1/me/watchlist/{news_id}` | [watchlist-remove.md](watchlist-remove.md) | 未着手 |

### Phase 3: 管理 CRUD (news_sources)

| # | Method | Path | ファイル | 状態 |
|---|---|---|---|---|
| 8 | GET | `/api/v1/sources` | [sources-list.md](sources-list.md) | 未着手 |
| 9 | POST | `/api/v1/sources` | [sources-create.md](sources-create.md) | 未着手 |
| 10 | DELETE | `/api/v1/sources/{source_id}` | [sources-delete.md](sources-delete.md) | 未着手 |
| 11 | PATCH | `/api/v1/sources/{source_id}/toggle` | [sources-toggle.md](sources-toggle.md) | 未着手 |

### Phase 4: 管理タスク (pipeline)

| # | Method | Path | ファイル | 状態 |
|---|---|---|---|---|
| 12 | POST | `/api/v1/pipeline/fetch` | [pipeline-fetch.md](pipeline-fetch.md) | 未着手 |
| 13 | POST | `/api/v1/pipeline/embed` | [pipeline-embed.md](pipeline-embed.md) | 未着手 |

## 各レビュードキュメントの書式

エンドポイント 1 本につき 1 ファイル。以下のセクションを含める。

```markdown
# <Method> <Path>

## 目的・要件
(このエンドポイントが満たすべきクライアントユースケース)

## 現状の実装
- Router: backend/app/routers/<file>.py:<line>
- Service: backend/app/services/<file>.py:<line>
- Repository: backend/app/repositories/<file>.py:<line>
- Schema: backend/app/schemas/<file>.py:<line>

(実装の要点を箇条書き)

## 観点別レビュー

### 1. 目的・要件
### 2. API 設計
### 3. スキーマ設計
### 4. クエリ効率
### 5. ロジック効率
### 6. 異常系・境界条件

## 論点と対処方針
| # | 論点 | 深刻度 | 対処方針 |
|---|---|---|---|

## 決定事項
(このレビューで確定した設計判断)
```

深刻度は `blocker / should-fix / nice-to-have / discuss` の 4 段階。

## 作業ブランチ

`review/endpoints` (main = f9a504b から分岐)。

各エンドポイントの修正は、まず当該ドキュメントに論点と対処方針を書き出し、合意してから実装に入る。小さな修正はまとめて1コミット、破壊的変更が混ざる場合はコミットを分ける。
