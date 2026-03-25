# スキーマ設計原則（セキュア・バイ・デザイン）

> 作成日: 2026-03-22
> 参考書籍: 『セキュア・バイ・デザイン』第2章 深いモデリング

このドキュメントは、Vector のスキーマ再設計を通じて確立した設計原則を記録する。
今後のエンティティ設計（Company, CustomFeed, TrendReport 等）にも同じ原則を適用すること。

## 1. ドメインの問い: 「この値は何か？」

属性の制約を定義するとき、「XSS を防ぐにはどうするか」ではなく「この値はそもそも何か？」を問う。

- CategoryName は「業界セクターの日本語名称」→ `[\w・ \-]+` が自然に導かれる
- KeywordName は「技術用語・テーマを表すタグ」→ `/`, `+`, `#`, `&` が必要（AI/ML, C++, C#, AT&T）
- ドメインの定義から許可文字が決まるのであり、セキュリティの都合で決めるのではない

## 2. 値オブジェクトで不変条件を体現する

プリミティブ型への執着（Primitive Obsession）を避け、値オブジェクトで「不正な値がそもそも存在できない」設計にする。

### 実装形式の使い分け

| 形式 | 基準 | 例 |
|------|------|-----|
| クラス | 複数のバリデーションルール（正規表現 + トリム + 長さ等） | `CategorySlug`, `CategoryName`, `KeywordName`, `NewsSourceName` |
| 型エイリアス（`Annotated` + `BeforeValidator`） | ライブラリ任せで自前ロジックが最小 | `HttpUrl`（Pydantic v2 `HttpUrl` + 長さ制約のみ） |

### 配置

- `backend/app/domain/` に値オブジェクトクラスを配置
- Pydantic の `Annotated` 型で統合

## 3. 多層防御

不変条件を単一のレイヤーに依存せず、複数レイヤーで防御する。

| レイヤー | 担当範囲 |
|----------|---------|
| **ドメイン層** | 値オブジェクトで文字種・形式を制約（内部データ） |
| **DB層** | CHECK 制約（enum 値、長さ）、UNIQUE、NOT NULL、FK RESTRICT/CASCADE |
| **アプリ層** | 値オブジェクトによるバリデーション、認可チェック、サニタイズ |
| **プロンプト層** | LLM 出力のバリデーション、デリミタ、システム指示（ArticleAnalysis） |
| **ネットワーク層** | BFF パターン、Docker internal ネットワーク、X-Internal-Secret |

### 各レイヤーの役割分担

- **文字種制御はアプリ層（値オブジェクト）**: `\w` の挙動が PostgreSQL のロケール設定に依存するため、DB CHECK に正規表現を入れない
- **DB 層は長さ・非空・UNIQUE・enum のみ**: 最終防衛線として機能。プロンプトインジェクションで LLM が不正な impact_level を返しても CHECK 制約が拒否する
- **FK は整合性の保証**: RESTRICT で親レコードの不正削除を防止、CASCADE で依存レコードの自動クリーンアップ

## 4. 内部データと外部データの区別

| 種類 | 作成者 | 防御方法 | 例 |
|------|--------|---------|-----|
| 内部データ | 管理者・システム | 値オブジェクトで文字種制限 | CategoryName, KeywordName, NewsSourceName |
| 外部データ | 外部ソース（RSS等） | サニタイズ・バリデーション | original_title, description_original, original_content |
| AI 出力 | LLM | 出力バリデーション + DB CHECK | impact_level, summary, reasoning |

内部データは「不正な値が存在できない」設計が可能。外部データはこちらが制御できないため、保存時の防御で対応する。

## 5. パイプライン状態は DB の外へ

ドメインの事実とパイプラインの一時状態を分離する。一時状態は DB に保存しない。

| 属性 | 移行先 | 理由 | エンティティ |
|------|--------|------|-------------|
| etag, last_modified_header | Redis | HTTP キャッシュの一時状態 | NewsSource |
| fetch_interval_minutes | 環境変数/config | デプロイ設定 | NewsSource |
| consecutive_errors, last_error_message | ワーカー/ログ | 一時的なエラー状態 | NewsSource |
| next_fetch_at | ワーカー/Redis | スケジューラの一時状態 | NewsSource |
| content_fetch_attempts | Redis キュー | リトライカウンタ | NewsArticle |
| content_fetched_at | 不要 | `original_content IS NOT NULL` で代替 | NewsArticle |
| guid | Redis | 重複排除キー。original_url UNIQUE が安全網 | NewsArticle |

### Redis キューによるリトライパターン

コンテンツ取得の失敗リトライは Redis キューで管理する:
1. 取得失敗 → `{article_id, attempt: 0}` をキュー末尾に追加
2. ワーカーが先頭から取り出して再取得
3. 再失敗 → attempt=0 なら `{article_id, attempt: 1}` で末尾に再追加
4. 再失敗 → attempt=1 ならそのまま破棄

Redis 再起動でキューが消えても、`description_original` がフォールバックとして機能し、分析は継続可能。

## 6. 事実は導出すべき

上書きされる値ではなく、元データから導出する。

| 導出対象 | 導出方法 | 理由 | エンティティ |
|---------|---------|------|-------------|
| last_fetched_at | `MAX(fetch_logs.fetched_at)` | 取得日時は事実であり上書きすべきでない | NewsSource |
| firstReportedAt | `MIN(published_at)` of grouped articles | 記事データから計算可能 | NewsEvent |
| reportCount | `COUNT(*)` of grouped articles + 期間指定 | 期間を区切らないと意味のある指標にならない | NewsEvent |
| collected_at | `created_at` で代替 | レコード作成 = 収集。別カラム不要 | NewsArticle |

## 7. 分析の産物は分析テーブルに

記事そのものの事実と、AI が生成した分析結果を分離する。

| 属性 | テーブル | 理由 |
|------|---------|------|
| embedding | ArticleAnalysis | AI が記事内容から生成したベクトル表現 |
| news_event_id | ArticleAnalysis（将来） | embedding 分析によるクラスタリング結果 |
| translated_title | ArticleAnalysis | LLM が生成した翻訳 |
| summary, impact_level, reasoning | ArticleAnalysis | LLM が生成した分析結果 |

判断基準: **その属性は記事が最初から持っているか、それとも分析して初めてわかることか？**

- 記事が持っている → NewsArticle（original_title, original_url, description_original, original_content, published_at）
- 分析の結果 → ArticleAnalysis（embedding, translated_title, summary, impact_level, reasoning）

## 8. 不変エンティティは updated_at を持たない

収集後・作成後に変更されないエンティティは `updated_at` を持たない。

| エンティティ | updated_at | 理由 |
|-------------|-----------|------|
| Category | あり | 管理者が name を変更し得る |
| Keyword | あり | status 遷移がある |
| NewsSource | あり | 管理者が name, is_active 等を変更し得る |
| NewsArticle | **なし** | 記事は収集された時点の事実。変更されない |
| ArticleAnalysis | **なし** | 分析結果は作成後に変更されない。新モデルは新記事に適用 |
| WatchlistEntry | **なし** | 保存操作の記録。変更されない |

## 9. サロゲートキー廃止の基準

以下の条件を全て満たす場合、サロゲートキー（`id` serial）を廃止し複合PK にする:

1. 自然キーの組み合わせで各行が一意に特定可能
2. 他のテーブルがこのテーブルを FK で参照しない
3. API のエンドポイント設計に支障がない

適用例:
- `watchlist_entries`: 複合PK `(user_id, news_article_id)`
- `article_keywords`: 複合PK `(news_article_id, keyword_id)`

## 10. FK 戦略

| 関係の性質 | ON DELETE | 例 |
|-----------|-----------|-----|
| 親が存在しないと子が成り立たない | CASCADE | 記事削除 → 分析結果も削除、ユーザー削除 → ウォッチリストも削除 |
| 子が存在する限り親を消すべきでない | RESTRICT | キーワードがある限りカテゴリは削除不可、記事がある限りソースは削除不可 |

SET NULL は使わない。全ての FK は NOT NULL であり、参照先が消えたら CASCADE で一緒に消すか、RESTRICT で削除を防止するかのどちらか。

## 11. auth スキーマとの連携

- User テーブルは Better Auth が `auth` スキーマで管理（Alembic 管轄外）
- `public` スキーマのテーブルは `auth.user(id)` を FK で参照する
- PostgreSQL はスキーマ跨ぎ FK をネイティブにサポート
- マイグレーション順序: Better Auth CLI → Alembic（この順序は既に確立済み）
- FK を張ることで、ユーザー削除時のデータ残留を防ぎ多層防御の原則に沿う

## 12. ドメインテーブルと運用テーブルの区別

テーブルを「ドメインテーブル」と「運用テーブル」に明確に区別する。設計基準が異なる。

| 区分 | 目的 | 設計基準 | 例 |
|------|------|---------|-----|
| ドメインテーブル | ビジネスの事実を記録 | セキュア・バイ・デザイン、値オブジェクト、不変条件 | news_articles, keywords, categories |
| 運用テーブル | システムの動作を記録 | 運用上の利便性を優先 | fetch_logs |

### 運用テーブルの設計基準

- ドメインの不変条件や値オブジェクトは適用しない
- サロゲートキー（id）はページネーションや順序保証のために残してよい
- ドメインテーブルと同じ DB に置くが、性質が異なることを認識する
- 将来ログ基盤（CloudWatch, ELK 等）を整備した段階で DB から除外を検討する

### fetch_logs の位置づけ

fetch_logs は運用テーブルとして DB に残す。2つの役割を持つ:

1. **クォータ管理**: Alpha Vantage の API リクエスト上限（25回/日）のカウント。Redis ではなく DB に持つ理由は、Redis 再起動でカウンタが消えるとクォータ超過のリスクがあるため
2. **運用監視**: 全ソースの取得結果の履歴記録（成功率、パフォーマンス、エラーパターン）

現時点ではログ基盤が存在しないため、DB が唯一の記録先として機能する。
