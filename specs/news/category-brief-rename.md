# CategoryDetail → CategoryBrief rename

> 作成日: 2026-06-10
> 対象: `backend/app/schemas/category.py` + `GET /api/v1/categories` + frontend sidebar 系
> Status: Accepted / not implemented
> 関連: [`briefing-schema-naming.md`](../insights/briefing-schema-naming.md) (記事/カテゴリ表現の三語彙原則。本件は
> briefing 刷新とは独立に実行可能なため spec を分離)

## 背景

repo の API スキーマは三語彙で整理されている:
Brief (一覧トップレベル、例 `ArticleBrief`) / Detail (詳細画面トップレベル、例
`ArticleDetail`) / Embed (他レスポンスへの埋め込み、`schemas/embeds.py`)。

`CategoryDetail` はこの語彙に違反している:

- 消費者は `GET /api/v1/categories` 経由の CategorySidebar / MobileSidebar /
  DashboardMasthead / PaperNewsResultSummary — すべてサイドバー・ダッシュボード系。
- `recent_count` (直近 24h の AI 分類完了数) はサイドバーの活動量表示用の集計。
- カテゴリの「詳細画面」はこのアプリに存在しない。

つまり実体は「一覧用のカテゴリ表現 + 集計」= **Brief 族**であり、"Detail" は
役割を誤って伝える名前。frontend は `types/index.ts` で手書きの
`CategoryBrief = Pick<CategoryDetail, "slug" | "name">` を既に作っており、
利用側が Brief という語彙を先に選んでいた実績もある。

## 決定 (2026-06-10 合意)

| 現行 | 変更後 |
|---|---|
| `CategoryDetail` | `CategoryBrief` |
| `CategoryDetailList` | `CategoryBriefList` |

- 破壊的変更 (生成型名の変更) は許容で合意済み。
- frontend の手書き `CategoryBrief` (Pick で slug + name) は中身が
  `CategoryEmbed` 相当のため、rename 時に整理する (利用箇所は `CategoryEmbed`
  直接参照へ)。これで category 表現は **Embed (参照) / Brief (一覧 + 集計)** の
  2 つに収束する。
- `schemas/embeds.py` 冒頭 docstring の例示が stale
  (`NewsBrief` は現存せず実体は `ArticleBrief`、`CategoryDetail` も本件で消える)
  なので同時に更新する。

## 実装時の手順

1. backend: `schemas/category.py` rename + `routers/categories.py` /
   `services/category.py` / `schemas/__init__.py` の追随、embeds.py docstring 更新
2. `/gen-types` 再生成
3. frontend: `get-categories.ts` / sidebar 系 component / `types/index.ts` の
   手書き `CategoryBrief` alias 整理
4. `/check`
