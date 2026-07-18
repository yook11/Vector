# 記事一覧カードの key point 化 仕様

Status: Implemented (PR #791)
工程: backend(ArticleBrief schema + build_brief) → /gen-types → frontend(PaperArticleCard) → /check

## 目的

記事一覧カードの主表示を summary 全文から key_points(投資判断に資する事実の箇条書き)へ切り替え、一覧のスキャン性を上げる。summary 全文は記事詳細の担当とし、一覧では使わない。

## 前提事実(実装に効くもののみ)

- 現役の一覧カードは `PaperArticleCard` 1本(ダッシュボード `/`・ウォッチリスト・記事詳細の関連記事 `RelatedArticles` で共有)。`NewsCard` は `NewsList` を呼ぶ page が無く dead code
- key_points は `in_scope_assessments.key_points`(JSONB, `[{content, mentions}]`)。NULL(旧行)/ `[]`(AI 未返却)/ values の3状態。共有関数 `extract_key_point_contents` が content のみ `list[str]` 化(記事詳細 API が使用中)
- dev 実測(in-scope 1370件):
  - key_points 空率 **12.3%**(= フォールバック発生頻度)
  - 1 key_point の文字数 avg **54** / p99 109 / **max 147**
  - 記事あたり件数 1〜2件 **62%** / 3件 21% / 4件以上 **4.6%**
  - 先頭3件の合計 avg **106字** ⇔ summary 全文 avg **503** / max 1873字
- 現状 `ArticleBrief` は `summary`(全文) を持ち、`PaperArticleCard` と `NewsCard` が参照

## API 契約(ArticleBrief 変更)

`ArticleBrief` の `summary: str`(全文) を廃止し、次の2フィールドへ置換する。

- `key_points: list[str]` — content のみ。**最大3件**。各 **最大250字**(超過時のみ末尾を省略記号で詰める)。空配列あり
- `summary_preview: str | None` — key_points が**空のときだけ** summary を最大300字でトランケートして返す。key_points が1件以上なら **null**。field 自体は常に存在する(Pydantic は `str | None` をデフォルト無しで required・nullable とし、serialize で null キーを省略しない)

- `build_brief`: `extract_key_point_contents(kp)[:3]` に各250字ガードを適用。key_points が**非空なら `summary_preview=None`、空なら** summary を300字ガードして設定する(相互排他をファクトリで構造的に保証する)
- 記事詳細 `ArticleDetail` は変更なし(summary 全文・key_points 全件 `list[str]` のまま)
- `mentions` は一覧・詳細とも API 非公開(既存方針)

## 表示仕様(PaperArticleCard)

- key_points が1件以上: 受け取った key_points を**全件**箇条書き表示(backend が最大3件を保証、frontend は追加 slice しない)。list は accessible name `要点` を持つ。各 clamp なし(実測 max 147字で破綻しない)
- key_points が空: `summary_preview` を表示(数行 line-clamp)
- カード高さは可変。既存の `h-full` + `mt-auto` によるフッター下端固定を維持
- 紙面デザインへの馴染みは実装時に frontend-ui-builder で調整

## Invariants

- 一覧 API は記事あたり key_point content を**最大3件・各250字以内**しか返さない
- `summary_preview` field は常に存在する(required・nullable)。値は相互排他:
  - key_points が**非空**の item は `summary_preview === null`
  - key_points が**空**の item は `summary_preview` が**非 null かつ300字以内**
- 相互排他(key_points 非空 ⟺ summary_preview null)は `build_brief` ファクトリで構造的に保証し、テストで検証する
- frontend は key_points があれば summaryPreview を表示せず、空なら summaryPreview を表示する(無言のカードを作らない)
- key_points の順序は**重要度を表さない**。先頭3件は便宜的選択(4件以上の切り捨ては実測 4.6%)
- key_points がある記事のペイロードは key_points のみ(summary_preview は null)で最小

##　満たすべき条件
API Contract

ArticleBrief から summary 全文は削除する
ArticleBrief は keyPoints と summaryPreview を返す
keyPoints は key_points[].content のみを返す
mentions は一覧・詳細とも API に出さない
一覧 API の keyPoints は最大3件
一覧 API の keyPoints は各250字以内
summaryPreview は最大300字
summaryPreview は field として常に存在する

表示仕様

PaperArticleCard は1つのまま維持する
カード全体を KeyPointCard / SummaryCard のように差し替えない
変えるのは本文エリアだけ
keyPoints.length > 0 なら key point list を表示する
keyPoints.length === 0 なら summaryPreview を表示する
key point がある場合、summaryPreview は表示しない
key point は最大3件なので frontend 側で追加削減しない
key point は原則 clamp しない
summary fallback は数行 clamp でよい
title / category / source / date / action slot / footer はどちらの表示でも維持する
無言のカードを作らない

## テスト観点(test-writer 用)

backend(`ArticleBrief` / `build_brief`):
- key_points がある item は `summary_preview is None`
- key_points が空の item は `summary_preview` が非 None かつ 300字以内
- key_points は最大3件、各 ≤250字
- `summary_preview` キーは常に出力される(required・nullable、省略されない)
- (router/HTTP) key_points 空のとき `summaryPreview` は summary フォールバック文字列、非空のとき null、`summary` 全文キーは無い


frontend(`PaperArticleCard`):
- keyPoints が非空: 各 content を表示し、list は accessible name `要点` を持つ
- keyPoints 非空 + summaryPreview に値があっても summaryPreview を表示しない(同一 render で keyPoint 表示も assert し discriminate する)
- keyPoints が空: summaryPreview を表示し `要点` list を描画しない
- frontend で件数キャップしない(3件制限は backend のみ。4件キャップテストは置かない)
- PaperArticleCard の中で本文スロットだけ差し替える実装にする


## Non-goals

- `NewsCard` / `NewsList` の削除・統合(型を通す最小追従修正のみ: `summary` 参照 → `summaryPreview`)
- key point の AI 生成・抽出ロジック変更
- key_points の重要度ソート導入(手段が無い)
- `mentions` の公開、記事詳細画面の表示変更

## Done

- 一覧カードが key_points 最大3件(各 ≤250字)を箇条書き表示、空なら `summaryPreview`(≤300字)
- `ArticleBrief` が summary 全文を返さなくなる
- `/gen-types` 反映済み、`/check`(backend + frontend)通過
