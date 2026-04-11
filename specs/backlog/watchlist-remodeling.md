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

## スコープ

**本ドキュメントは結論を出さない。** 設計選択肢と症状を記録するだけに留める。
後続ブランチで以下を決定する：

1. どの方向性（A/B/C あるいは別の選択肢）を採用するか
2. 採用した方向性に沿ってどこまで Vector の他エンティティの relationship を
   見直すか
3. Aggregate 境界を Vector 全体に広げるか、watchlist に閉じるか

## 関連論点

- `specs/backlog/transaction-boundary-and-rich-model.md` 論点3
  （Rich Domain Model の適用範囲）
  - 「エンティティ単位で Rich Model 化するのか、Aggregate 境界で考えるのか」
    という問いが自然に浮上する。本 watchlist の議論はその前置き
- 論点2-C（`create` と `save` の分離・統合）
  - 本 watchlist の例外運用を認めず、C-1 は「例外なしの規約」として決着させる
  - watchlist は「現状は規約の射程外、後続ブランチで本質的に解決」として棚上げ

## 進め方（2026-04-12 更新）

論点3 を 2 軸に分割し、本 watchlist 再モデリングは **論点3-X** として
**論点3-Y（状態機械エンティティの Rich Model 化）の完了後** に着手する
順序が確定した。

### 順序

1. 現在進行中の `refactor/news-source-activation` ブランチでは watchlist に
   触らない
2. **論点3-Y の実装フェーズ**: `news_source` の Rich Model 化 PR で
   Rich 化の規約・`save` の意味論を確立する
3. その経験を `Keyword` などの他の状態機械エンティティに展開
4. **論点3-Y 完了後**、本ドキュメントを起点に watchlist の再モデリングに着手
5. 確立済みの規約（`save` 意味論、Model ドメインメソッドの書き味）を **前提**
   として、watchlist では **モデリング問題の修正だけに集中** する
6. 方向性が決まり次第、本ドキュメントを `specs/` 直下に昇格して具体的な
   移行計画に展開する

### この順序の理由

watchlist には2つの独立した問題が同時に乗っている：

1. モデリング問題: `WatchlistEntry` が中途半端な独立エンティティとして
   設計されている
2. Rich 化問題: 「watch する/しない」の判断ロジックの置き場所

watchlist 先だとこの2つが同じ議論で混ざり、Rich 化の道具がまだ無い状態で
「User Aggregate 的に `user.watch()` を作るべきか」を議論することになる。
論点3-Y を先にやって Rich 化の道具を確立してから降りれば、watchlist 議論
は **モデリング問題だけに集中** できる。

watchlist は機能的バグではなくモデリングの歪みなので、論点3-Y 完了まで
現状放置で害は出ない。

## 参考

- 議論の発端: `refactor/news-source-activation` ブランチの論点2-C 議論
- 関連 memory:
  - `project_transaction_boundary_discussion` — 論点2-C の文脈
  - `feedback_layer_architecture` — レイヤー責務分離の原則
  - `feedback_schema_design` — ストレージ構造を API に露出しない原則
