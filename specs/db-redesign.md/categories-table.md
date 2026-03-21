# Categories テーブル設計（セキュア・バイ・デザイン）

> 作成日: 2026-03-21
> ソース: `specs/db-domain-model.md` セクション 2.5 Category
> ギャップ分析: GAP-1（investment_categories 削除、keyword_categories → categories リネーム）、GAP-6（Keyword との関係を M:N → 1:N）

## 1. 概要

業界分類の大枠を表す固定マスタ。10個程度の事前定義セクター（AI/ML、半導体、バイオテック等）。
Keyword の所属先であり、記事のセクターは Keyword 経由で導出される（直接 M:N を持たない）。

### 現行 → 新設計の変更点

| 項目 | 現行 | 新設計 |
|------|------|--------|
| テーブル名 | `keyword_categories` | `categories` |
| 翻訳 | `keyword_category_translations`（別テーブル） | 廃止。`name` を直接属性に |
| Keyword との関係 | M:N（`keyword_category_links` 中間テーブル） | 1:N（Keyword 側に `category_id` FK） |
| `investment_categories` | 存在（6種） | 削除 |

## 2. 属性の不変条件

### id

| 項目 | 定義 |
|------|------|
| 型 | Integer (AUTO INCREMENT) |
| DB制約 | PRIMARY KEY |
| 不変条件 | 自動採番、変更不可 |

### slug

| 項目 | 定義 |
|------|------|
| 型 | VARCHAR(50) |
| DB制約 | `NOT NULL`, `UNIQUE`, `CHECK (char_length(trim(slug)) >= 1 AND char_length(slug) <= 50)` |
| 不変条件 | 英小文字・数字始まり、英小文字・数字・アンダースコアのみ、1-50文字 |
| 許可文字 | `^[a-z0-9][a-z0-9_]{0,49}$` — アプリ層（値オブジェクト）で検証 |
| 備考 | 作成後の変更不可（URL パス・API フィルタパラメータ・シードデータの識別子として使用）。数字始まりを許容する理由: `5g_telecom`, `3d_printing` 等のカテゴリが現実的にあり得る。Integer ID との判別は Integer パース可否で一意に決まるため曖昧性なし。DB CHECK は長さと非空のみ保証し、フォーマット検証は値オブジェクトに委譲（全テーブル共通方針） |

### name

| 項目 | 定義 |
|------|------|
| 型 | VARCHAR(50) |
| DB制約 | `NOT NULL`, `UNIQUE`, `CHECK (char_length(trim(name)) >= 1 AND char_length(name) <= 50)` |
| 不変条件 | UI表示名、空白文字のみ不可、1-50文字 |
| 備考 | 日本語表示名（例: 「AI・ML」「半導体」「バイオテクノロジー」）。多言語対応しないため翻訳テーブル不要。50文字の根拠: 日本語カテゴリ名は最長でも20文字程度、英語でも "Renewable Energy & Sustainability" で34文字。100文字は過剰 |

## 3. エンティティレベルの不変条件

| 制約 | 実現レイヤー | 説明 |
|------|-------------|------|
| 管理者のみ CRUD 可能 | アプリ層（認可） | 一般ユーザーは参照のみ |
| Keyword が紐づく Category は削除不可 | DB層（FK RESTRICT） | Keyword.category_id の ON DELETE RESTRICT で強制 |
| slug は作成後変更不可 | アプリ層（更新API で slug フィールドを受け付けない） | DB層では制約不要（UPDATE 自体を許可しない設計） |

## 4. 多層防御サマリ

| レイヤー | 防御内容 |
|----------|---------|
| **ドメイン層** | slug: URL安全な識別子、name: 空でない表示名、slug は不変 |
| **DB層** | CHECK制約（slug長さ、name長さ）、UNIQUE（slug, name）、NOT NULL、FK RESTRICT |
| **アプリ層** | Pydantic バリデーション（型・長さ・正規表現）、認可チェック（admin only）、slug 変更禁止 |

## 5. シードデータ（現行から移行）

| slug | name |
|------|------|
| ai_ml | AI・ML |
| biotech | バイオテクノロジー |
| energy | エネルギー |
| fintech | フィンテック |
| materials | 素材・材料 |
| quantum | 量子コンピューティング |
| robotics | ロボティクス |
| semiconductor | 半導体 |
| space | 宇宙 |
| telecom | 通信 |
