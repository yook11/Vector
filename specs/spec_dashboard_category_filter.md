# ダッシュボード カテゴリフィルタ改修 仕様書

## 背景

現状のダッシュボードはユーザーが自由にキーワードを追加し、そのキーワードで RSS を検索する設計になっている。Phase 3A で以下の方針に転換する：

- **記事取得はシステムの責務**（taskiq スケジューラが定期実行）
- **ユーザーは取得済み記事を閲覧・フィルタリングする**
- **フィルタは大分類（keyword_categories）→ 小分類（keywords）のドリルダウン**

## 変更概要

| 変更 | 現状 | あるべき姿 |
|------|------|-----------|
| サイドバー | Keywords 一覧（ユーザー追加） | Categories → Tags ドリルダウン |
| Fetch News ボタン | Dashboard に表示 | 非表示（管理者/デバッグ用に API は残す） |
| Settings キーワード追加 | 自由入力フォーム | 非表示（将来復活の可能性あり） |
| 記事フィルタ | keyword_id でフィルタ | category_id + keyword_id でフィルタ |

---

## 1. バックエンド変更

### 1-1. GET /api/v1/news — クエリパラメータ追加

現状の `keyword` パラメータに加え、`category_id` パラメータを追加する。

```
GET /api/v1/news?category_id=1              → ai_ml カテゴリの全記事
GET /api/v1/news?category_id=1&keyword_id=3 → ai_ml の中で keyword_id=3 の記事
GET /api/v1/news                            → 全記事（フィルタなし）
```

クエリロジック：
- `category_id` 指定時: `keyword_category_links` を JOIN して該当カテゴリの記事を取得
- `keyword_id` 指定時: 既存ロジック（`news_keywords` JOIN）で取得
- 両方指定時: AND 条件

### 1-2. GET /api/v1/keyword-categories — 新規エンドポイント

カテゴリ一覧とその配下のキーワードを返す。サイドバーの描画に使用。

```
GET /api/v1/keyword-categories
```

レスポンス:
```json
{
  "items": [
    {
      "id": 1,
      "slug": "ai_ml",
      "name": "AI・機械学習",
      "articleCount": 45,
      "keywords": [
        { "id": 1, "keyword": "large language model", "articleCount": 20 },
        { "id": 2, "keyword": "generative AI", "articleCount": 15 },
        ...
      ]
    },
    ...
  ]
}
```

備考:
- `name` は `keyword_category_translations` テーブルから日本語名を取得
- `articleCount` は `news_keywords` + `keyword_category_links` を JOIN して集計
- キーワード配下の `articleCount` は該当キーワードに紐づく記事数

### 1-3. POST /api/v1/news/fetch — 変更なし

API 自体は残す。UI からの呼び出しを外すだけ。将来的に管理者ロールを追加した際にアクセス制御を入れる候補。

### 1-4. POST /api/v1/keywords — 変更なし

バックエンドは残す。フロントエンドの UI を非表示にするだけ。

---

## 2. フロントエンド変更

### 2-1. サイドバー — Categories ドリルダウン

**現状**: Keywords 一覧（All + 各キーワード）

**変更後**:
```
Categories
├── All                        ← 全記事表示
├── AI・機械学習 (45)           ← クリックでカテゴリ全体をフィルタ
│   ├── large language model (20)  ← クリックで小分類フィルタ
│   ├── generative AI (15)
│   └── ...
├── 量子技術 (30)
│   ├── quantum computing (12)
│   └── ...
└── ...
```

動作:
- 大分類クリック → URL パラメータ `?category_id=1` → 配下の全記事表示
- 小分類クリック → URL パラメータ `?category_id=1&keyword_id=3` → タグで絞り込み
- 大分類は折りたたみ/展開（デフォルトは折りたたみ）
- 「All」クリック → パラメータなし → 全記事表示

データ取得:
- `GET /api/v1/keyword-categories` でカテゴリ + キーワード一覧を取得
- 記事数はレスポンスに含まれる `articleCount` を表示

### 2-2. Fetch News ボタン — 非表示

`FetchButton.tsx` のレンダリングを削除またはコメントアウト。コンポーネントファイル自体は残す（将来の管理者画面で使用する可能性）。

### 2-3. Settings ページ — キーワード追加フォーム非表示

キーワードの自由入力フォームを非表示にする。コンポーネントファイルは残す。

### 2-4. フィルタバー — 既存フィルタとの共存

Dashboard 上部のフィルタ（Sentiment, Sort by, Category, Order）は維持。サイドバーのカテゴリ選択と `Category` ドロップダウンの役割が重複する場合は、ドロップダウンの `Category` を削除するか、サイドバー選択と連動させる。

---

## 3. URL 設計

```
/dashboard                               → 全記事
/dashboard?category_id=1                 → ai_ml カテゴリ
/dashboard?category_id=1&keyword_id=3   → ai_ml の keyword_id=3
/dashboard?category_id=1&sentiment=positive → ai_ml + ポジティブ
```

既存の `sentiment`, `sort_by`, `order` パラメータはそのまま維持。`category_id` と `keyword_id` を追加。

---

## 4. 実装順序

| # | タスク | スコープ |
|---|--------|----------|
| 1 | GET /api/v1/keyword-categories エンドポイント追加 | Backend |
| 2 | GET /api/v1/news に category_id フィルタ追加 | Backend |
| 3 | generated.ts 再生成 | Frontend |
| 4 | サイドバーをカテゴリドリルダウンに置き換え | Frontend |
| 5 | Fetch News ボタン非表示 | Frontend |
| 6 | Settings キーワード追加フォーム非表示 | Frontend |
| 7 | フィルタバーの Category ドロップダウン調整 | Frontend |

---

## 5. 対象ファイル（想定）

| ファイル | 変更内容 |
|----------|----------|
| backend/app/routers/keyword_categories.py | 新規: GET /keyword-categories |
| backend/app/schemas/keyword_category.py | 新規 or 拡張: レスポンススキーマ |
| backend/app/routers/news.py | category_id クエリパラメータ追加 |
| frontend/src/types/generated.ts | 再生成 |
| frontend/src/components/Sidebar.tsx | カテゴリドリルダウン UI |
| frontend/src/components/FetchButton.tsx | 非表示化 |
| frontend/src/app/dashboard/page.tsx | サイドバー連携 |
| frontend/src/app/settings/page.tsx | キーワード追加フォーム非表示 |

---

## 6. スコープ外（将来対応）

- ユーザーごとのカテゴリ購読設定（user_category_subscriptions）
- ユーザーによるカスタムタグ追加
- AI によるキーワード自動提案・拡充
- 管理者ロールとアクセス制御
- フィード購読型への取得方法移行（3A-1）