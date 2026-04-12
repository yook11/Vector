# Watchlist の再モデリング

> 作成日: 2026-04-11
> ステータス: backlog（設計検討）
> 契機: `specs/backlog/transaction-boundary-and-rich-model.md` 論点2-C の議論中に
> Yk さんの「WatchlistEntry はただの関係の記録である」という洞察から派生

## 問題意識

Vector の `WatchlistEntry` は **中途半端に独立したエンティティ** として
設計されており、Rich Domain Model 化と Repository 規約の整備を進めるにあたり、
以下の矛盾が顕在化している。

「完全に誤り」ではなく「独立エンティティとしての設計と、関係の記録としての
実質がちぐはぐになっている」という状態。本ドキュメントは症状を整理し、
後続ブランチで本質的に解決するための足がかりとする。

## 現状の姿

### すでに「関係の記録」として振る舞っている箇所

- API スキーマには `WatchlistEntry` が露出していない
- 複合主キー `(user_id, article_analysis_id)` を採用し、独自 id を持たない
  → すでに「タプル的 identity」としての設計
- Repository は primitives（`user_id`, `article_id`）を受け取るインターフェイス
- Service も primitives で扱う（`add_to_watchlist(user_id, article_id)`）

### 一方で独立エンティティの痕跡が残っている箇所

- `WatchlistEntry` という独立 Model ファイルが存在
- `ArticleAnalysis.watchlist_entries` という逆方向 relationship が存在
- テスト conftest で直接 `WatchlistEntry` をインスタンス化して利用

## 特定した症状

以下はすべて「半独立状態」に由来する構造的な歪み。個別の bug ではなく
モデリング選択の帰結である。

### 症状1: Rich Model の視点で宙ぶらりん

- `NewsSource.activate()` のような状態遷移を持たない
- 独自のドメインルールを持たない
- つまり Rich Model 化の対象にすべきか非対象にすべきかが曖昧
- 論点2-C（`create` と `save` の議論）で「エンティティの一般ルール」に
  乗せると不自然になり、例外扱いせざるを得ない

### 症状2: ArticleAnalysis 側の逆方向 relationship

- `ArticleAnalysis.watchlist_entries` という relationship がある
- 実質的には「記事側が watchlist を知っている」構造
- ドメイン的には逆が自然: 「ユーザーが記事を watch する」のであって
  「記事が自分を watch しているユーザーたちを知っている」必要はない
- この relationship を通じて watchlist の存在が記事ドメインに漏れている

### 症状3: User 側に何もない

- ウォッチリストの所有者は User なのに、User 側から watchlist を辿る仕組みがない
- `user.is_watching(article_id)` のようなドメイン語彙が存在しない
- 「watchlist は誰のものか」という問いに対する答えがコード構造上に無い

### 症状4: WatchlistRepository が Article を JOIN する構造

- `fetch_watched_articles` は watchlist 視点の関数なのに、内部では
  `ArticleAnalysis` を主語にクエリを組む（`article_eager_options_brief` も使う）
- watchlist Repository の責務と article Repository の責務の境界が曖昧
- 記事一覧の取得ロジックと watchlist の取得ロジックが相互参照している

### 症状5: is_watched / watched_among が Article 層に漏れ出している

- `article_eager_options_brief` が呼ばれる文脈で「この記事は watch 済みか？」を
  後付けする構造になっている
- 記事一覧 API の組み立て処理が watchlist の存在を知っている
- `feedback_schema_design` の「ストレージ構造を API に露出しない」と
  同じ系統の問題: watchlist というストレージ上の概念が、本来関係ない
  ドメイン（記事一覧）に漏れている

## 設計方向性の仮説（未決・後続ブランチで具体化）

### 方向性 A: WatchlistEntry を完全に隠蔽した relation として扱う

- `WatchlistEntry` 独自 Model を廃止または internal 化
- SQLAlchemy の `secondary` を使った純粋な many-to-many relationship として
  User と ArticleAnalysis を結ぶ
- 「関係の記録」であることを構造に反映
- 課題: `created_at`（watch した時刻）をどう扱うか、順序ソートをどう実現するか

### 方向性 B: User を Aggregate Root とした Rich Model 化

- `user.watch(article_id)`, `user.unwatch(article_id)`, `user.is_watching(article_id)`
  のようなドメインメソッドを User に持たせる
- WatchlistEntry は User Aggregate 内部のデータ
- 不変条件（重複禁止）が User の内的ルールとして自然に表現される
- 課題:
  - User をロードするたびに watchlist も一緒にロードするのか
  - ロードせずに `user.watch()` を実装する工夫（UNIQUE 制約に委ねる等）が必要
  - Aggregate 境界を他エンティティにも拡張するか（ArticleAnalysis, NewsArticle 等）

### 方向性 C: watchlist を独立した Bounded Context として切り出す

- watchlist を User や Article とは別の文脈として扱う
- Watchlist という Aggregate Root を作り、その中に Entry を含める
- 症状5（is_watched の記事 API への漏れ）を「context 間の明示的な問い合わせ」
  として再設計する
- 課題: Vector 全体で Bounded Context を扱うのは今の規模だと重すぎる可能性

## 決着 (2026-04-12)

### 方向性の結論

**方向性 A/B/C はいずれも採用しない。理想の解は「watchlist を BFF 側に移す」。
ただし変更規模が大きく、ビジネス優先度が低いため、今は実装しない。**

### 議論の経緯

論点3-Y (Rich Model 化) 完了後に本議論に着手。以下の順で検討した：

1. **症状の再確認**: 症状1〜5 はすべて実コードで確認。特に問題α
   (ArticleService → WatchlistRepository の依存) と問題β
   (WatchlistRepository の越境クエリ) が本質的な構造問題
2. **前提の確立**: 「ウォッチリストの本質はユーザーがお気に入り記事を
   登録する操作」「ウォッチリストはユーザーごとに個別」「Article は
   Watchlist を知らない」
3. **方向性 A (純粋 relation 化)**: 「ウォッチリスト」という語彙が消える。
   ドメインの実態を薄めるので却下
4. **方向性 B (User Aggregate Root)**: Vector 側に User Model が存在
   しないため成立しない。`auth_ref.py` は FK 解決用の `Table()` のみで
   Mapped class ではない
5. **BFF 移行案**: watchlist を BFF 側に移す案。ドメイン境界として
   一貫性があり、問題α/β が前提ごと消える。しかし「BFF が DB を
   直で叩くなら backend と変わらない」「URL リソースで表現するのが
   複雑」という疑問が出た
6. **B の変形 (auth_ref を Mapped class 化)**: テーブル新設せず
   `user.add_to_watchlist()` が書ける。Alembic は `auth` スキーマ
   除外済みで安全。技術的には実現可能だが、書き込み経路が整理されても
   読み取り経路 (問題α) は残る。Vector 内でどう工夫しても、記事
   レスポンスに `is_watched` を埋める合成をどこかでやる限りシワが寄る
7. **最終判断**: BFF が表示用データを合成する責務を持つのが最も自然。
   watchlist (ユーザー個人の状態) は認証と同じカテゴリで、BFF 側に
   属するのが正しい。ただし変更規模が大きく、ビジネス優先度が低い
   ため、今は実装しない

### 根本原因の発見

watchlist の議論が複雑化した原因は watchlist 自体ではなく、**Vector 側に
User Model が存在しない**という既存の未整理を watchlist が表面化させた
ことにある。Vector は「ユーザーがコンテンツに対して操作する機能」を持つ
のが初めてで、「所有していない User を主語にする振る舞い」が構造上
表現できなかった。

### 現状の許容

以下の歪みは認識した上で許容する：

- 問題α: ArticleService が WatchlistRepository に依存 → 残置
- 問題β: WatchlistRepository が article テーブルを JOIN → 残置
- 症状2: ArticleAnalysis.watchlist_entries の逆 relationship → 残置
- WatchlistEntry の Rich 化対象外 → ADR-004 のカテゴリ外 (関係記録) なので
  そもそも問題ではない

### 再開条件

以下のいずれかの場合に本 spec を再開する：

- BFF 側の機能を充実させる判断が下りたとき
- watchlist の機能拡張が必要になったとき
- 「ユーザーがコンテンツに対して操作する」新機能 (コメント、評価等) を
  追加するとき

再開時は「watchlist を BFF に移す」を前提に、BFF 側の DB 構成
(同一 Postgres のスキーマ分離) を活かした設計を行う。

### ビジネス価値の判断

watchlist はビジネス的に重要度の低い周辺機能。Vector の価値の中心は
「海外テックニュースの AI 分析」であり、watchlist の完璧さよりも
本質的な機能開発に投資すべき。この判断を明示的に記録する。

## 関連論点

- `specs/backlog/transaction-boundary-and-rich-model.md` 論点3
- `docs/adr/004_unit_of_work_service_convention.md` — ADR-004 は
  状態機械エンティティが対象。WatchlistEntry (関係記録) は射程外

## 参考

- 議論の発端: `refactor/news-source-activation` ブランチの論点2-C 議論
- 関連 memory:
  - `project_transaction_boundary_discussion` — 論点2-C の文脈
  - `feedback_layer_architecture` — レイヤー責務分離の原則
  - `feedback_schema_design` — ストレージ構造を API に露出しない原則
