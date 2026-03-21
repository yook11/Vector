# Keywords テーブル設計（セキュア・バイ・デザイン）

> 作成日: 2026-03-21
> ソース: `specs/db-domain-model.md` セクション 2.6 Keyword
> ギャップ分析: GAP-5（status/approved_at 追加）、GAP-6（Category との関係を M:N → 1:N）

## 1. 概要

セクター内の具体的な技術・テーマを表すタグ。
AIが記事から自動検出し、管理者が承認するワークフローを持つ。初期キーワードは事前定義（official）、以降はAI検出（provisional）+ 管理者承認で追加。

### 現行 → 新設計の変更点

| 項目 | 現行 | 新設計 |
|------|------|--------|
| `keyword` カラム | VARCHAR(200) | `name` にリネーム、VARCHAR(100) |
| Category との関係 | M:N（`keyword_category_links` 中間テーブル） | 1:N（`category_id` FK） |
| `status` | なし | `provisional / official / blacklisted` 追加 |
| `detected_at` | なし | 廃止（`created_at` + `is_ai_generated` で代替） |
| `approved_at` | なし | 追加 |
| `is_ai_generated` | なし | 追加 |

## 2. 属性の不変条件

### id

| 項目 | 定義 |
|------|------|
| 型 | Integer (AUTO INCREMENT) |
| DB制約 | PRIMARY KEY |
| 不変条件 | 自動採番、変更不可 |

### name（現行 `keyword` からリネーム）

| 項目 | 定義 |
|------|------|
| 型 | VARCHAR(100) |
| DB制約 | `NOT NULL`, `UNIQUE`, `CHECK (char_length(trim(name)) >= 1 AND char_length(name) <= 100)` |
| 値オブジェクト | `KeywordName` クラスで不変条件を体現 |
| 不変条件 | 技術用語・テーマ名。トリム後1文字以上、1-100文字 |
| 許可文字 | `^(?=.*\w)[\w \-\.&/+#]+$`（Unicode）|
| 文字種の根拠 | `\w` で日英の文字・数字。記号は技術表記で必要なもの: `/`（AI/ML）、`+`（C++）、`#`（C#）、`.`（Node.js）、`&`（AT&T）、`-`（e-commerce） |
| 備考 | 文字種制御は値オブジェクト（アプリ層）で実現。DB層は長さと非空のみ保証。理由: 許可文字セットの変更のたびに Alembic マイグレーションが必要になるのを避ける |

### category_id

| 項目 | 定義 |
|------|------|
| 型 | Integer |
| DB制約 | `NOT NULL`, `FOREIGN KEY REFERENCES categories(id) ON DELETE RESTRICT` |
| 不変条件 | 全キーワードは必ず1つのカテゴリに所属する |
| 備考 | RESTRICT により、Keyword が存在する Category の削除を防止 |

### status

| 項目 | 定義 |
|------|------|
| 型 | VARCHAR(20) |
| DB制約 | `NOT NULL`, `DEFAULT 'provisional'`, `CHECK (status IN ('provisional', 'official', 'blacklisted'))` |
| 不変条件 | 3値のいずれかのみ。状態遷移ルールはアプリ層で強制 |

#### 状態遷移（確定）

```
管理者が手動作成 → official（管理者が作る以上、最初から正式）
AI検出 → provisional（暫定タグ。管理者の判断待ち）
  ├→ 管理者が承認 → official
  ├→ 管理者がマージ → KeywordSynonym として既存に紐づけ（レコード自体を削除）
  └→ 管理者が除外 → blacklisted

official → blacklisted（正式化後に誤りに気付いた場合）
```

#### 許可しない遷移

| 遷移 | 理由 |
|------|------|
| blacklisted → provisional | 復帰不可。再出現防止が目的。必要なら削除して新規作成する |
| official → provisional | 格下げ不可。取り消すなら blacklisted |

### is_ai_generated

| 項目 | 定義 |
|------|------|
| 型 | Boolean |
| DB制約 | `NOT NULL`, `DEFAULT false` |
| 不変条件 | 作成後変更不可。AIが生成したか、管理者が手動作成したかの出自を記録する監査属性 |

#### なぜこの属性が必要か（設計判断の記録）

当初は `detected_at`（AI検出日時）を設けていたが、以下の分析により `is_ai_generated: bool` に置き換えた。

**問題**: provisional → official に遷移した後、元がAI検出だったという情報が status だけでは判別できなくなる。

**必要な場面**: AIが検出したキーワードが画面に表示され、それが不適切な内容だった場合の原因追跡。
- AI検出 → 未レビューで表示された（provisional のまま） → システムの問題
- AI検出 → 管理者が承認した（official） → 承認プロセスの問題
- 管理者が手動作成した → 管理者の判断の問題

出自（人かAIか）によって責任の所在が変わるため、status が遷移しても消えない不変の記録が必要。

**`detected_at` を廃止した理由**:
- 知りたいのは「いつ検出されたか」ではなく「誰が作ったか（人かAIか）」
- 作成日時は `created_at` で記録済み
- bool の方が意図が明確

### approved_at

| 項目 | 定義 |
|------|------|
| 型 | TIMESTAMP WITH TIME ZONE |
| DB制約 | NULL 許容 |
| 不変条件 | status が `official` の場合のみ非NULL |
| 遷移時の振る舞い | provisional → official: 承認日時を設定。official → blacklisted: NULL に戻す（「現在の承認状態」を表す。履歴は求めない） |
| 備考 | シードデータおよび管理者手動作成は `official` だが承認ワークフローを経ていないため `approved_at = NULL` |

### created_at

| 項目 | 定義 |
|------|------|
| 型 | TIMESTAMP WITH TIME ZONE |
| DB制約 | `NOT NULL`, `DEFAULT NOW()` |
| 不変条件 | レコード作成日時。変更不可 |
| 備考 | 自動非表示ルールの起算点（provisional のまま `created_at` から一定期間経過 → 表示除外） |

### updated_at

| 項目 | 定義 |
|------|------|
| 型 | TIMESTAMP WITH TIME ZONE |
| DB制約 | `NOT NULL`, `DEFAULT NOW()` |
| 不変条件 | レコード更新日時。全更新で自動更新 |
| 実現方法 | アプリ層（ORM イベント / 全テーブル共通の TimestampMixin）で自動更新。`DEFAULT NOW()` は INSERT 時のみ有効 |

## 3. エンティティレベルの不変条件

| 制約 | 実現レイヤー | 説明 |
|------|-------------|------|
| 管理者のみ作成・更新・削除可能 | アプリ層（認可） | AI検出はシステム内部処理として作成 |
| `status` と `approved_at` の整合性 | アプリ層 | official 時のみ approved_at を設定、blacklisted 遷移時に NULL に戻す |
| 状態遷移ルールの強制 | アプリ層 | 許可しない遷移を拒否 |
| 自動非表示 | アプリ層（クエリフィルタ） | provisional かつ `created_at` から一定期間経過 → 表示除外。ステータスは変更しない |
| blacklisted の再検出防止 | アプリ層 | AI検出時に blacklisted の name と照合し、一致したら無視 |

## 4. 多層防御サマリ

| レイヤー | 防御内容 |
|----------|---------|
| **ドメイン層** | `KeywordName` 値オブジェクト（技術用語の文字種制約）、status 状態遷移ルール、is_ai_generated 不変性 |
| **DB層** | CHECK制約（status enum、name長さ）、UNIQUE（name）、NOT NULL、FK RESTRICT（category_id） |
| **アプリ層** | 値オブジェクトによるバリデーション、認可チェック（admin only）、状態遷移の強制、自動非表示フィルタ |

## 5. 値オブジェクト

### KeywordName

| 項目 | 定義 |
|------|------|
| ドメイン定義 | セクター内の具体的な技術・テーマを表すタグ名 |
| 許可文字 | `^(?=.*\w)[\w \-\.&/+#]+$`（Unicode）|
| 長さ | 1-100文字（トリム後） |
| トリム | 先頭・末尾の空白を自動除去 |
| 例 | "large language model", "量子エラー訂正", "AI/ML", "C++", "Node.js" |
| 入るべきでないもの | 文章、HTML、制御文字 |
