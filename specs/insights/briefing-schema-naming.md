# Briefing response schema の命名・形状刷新

> 作成日: 2026-06-10 (同日更新: keyArticles の形状刷新・Embed 命名ほかを追加)
> 対象: `backend/app/insights/briefing/schemas.py` + `errors.py` + frontend briefing feature
> Status: Implemented (PR #784, #787, #789)

## 背景 1: クラス名は契約の一部

`schemas.py` の `_Xxx` クラスは module private に見えるが、実際には openapi 経由で
frontend の public 型名として露出する。

- `_` prefix は `@hey-api/openapi-ts` の生成時に剥がれ、`_ChapterOut` は
  `frontend/src/types/types.gen.ts` に `export type ChapterOut` として出る。
- つまり **schema クラス名 = frontend に見せる public 契約名** であり、
  「内部名だから雑でもよい」は成り立たない。

現状の問題:

1. **"Out" suffix は契約面で無意味。** 「出力方向」は `briefing/schemas.py` という
   居場所と `_` prefix が既に担っており、frontend に渡った `ChapterOut` の
   "Out" は読み手に何も伝えない。
2. **trends 側と非対称。** trend_discovery は命名刷新済みで `_RankedMention` /
   `_CategoryTrends` (Out なし)。briefing の Out は刷新の取り残し。
3. **frontend が名前を拒否している実績。** `frontend/src/types/index.ts` に
   「Pydantic 内部 schema 名 → frontend public 名」の手書き alias rename 層があり、
   `BriefingKeyArticle = KeyArticleOut` 等と補正している。利用側が付け直さないと
   使えない名前は命名の失敗。trends の `RankedMention` は alias なしで
   そのまま re-export されており、正しく命名すれば補正層が不要なことの対照になる。

## 背景 2: keyArticles / articles[] の並列 2 配列は backend 都合の漏出

`ReadyBriefing` は `keyArticles[]` (articleId + significance、briefing JSONB の鏡像)
と `articles[]` (記事事実の lookup table、DB join の結果) を並列で返し、frontend が
`articlesById` Map を構築して join している (`BriefingDocument.tsx` で Map 構築 →
props 引き回し → `KeyArticleBlock.tsx` で get + `article &&` ガード)。

- これは backend が「別々に作っている」構造がそのまま契約に出た形。契約は
  利用側の概念に合わせるべきで、利用側にとって重要記事は
  「id + なぜ重要か + 記事の事実」が揃った 1 つの塊。
- lookup table が正当化されるのは response 内の複数箇所が同一記事を参照し
  重複排除が要る場合だが、参照は keyArticles のみで、しかも domain validator が
  article_id 一意を保証済みのため dedup の利得は構造的にゼロ。
- join miss 時の扱いが frontend の `article &&` 分岐に暗黙に置かれ、
  「significance だけ表示」という半端な状態が契約上どこにも明示されていない。

## 原則 (合意済み)

1. **`_Xxx` の `Xxx` は「frontend に見せたい public 契約名」そのものとして命名する。**
   "Out" suffix は廃止。生成型は global flat namespace に出るため、無文脈な一般語を
   避け自己記述する名前を付ける。domain 語彙にない語を型名に持ち込まない。
2. **記事表現は repo の三語彙に従う。** Brief (一覧トップレベル、`ArticleBrief`) /
   Detail (詳細トップレベル、`ArticleDetail`) / Embed (他レスポンスへの埋め込み、
   `schemas/embeds.py`)。
3. **出口契約は JSONB・組み立て手順の鏡像である義務がない。** 利用側の概念形で返す。

## 決定済み (2026-06-10 合意)

### 命名

| 現行 | 変更後 |
|---|---|
| `_ChapterOut` | `_BriefingChapter` |
| `_KeyArticleOut` | `_BriefingKeyArticle` (形状も変更、下記) |
| `_ArticleSummaryOut` | `_BriefingArticleEmbed` (形状も変更、下記) |
| `_WatchPointOut` | 削除 — `watch_points: list[str]` へ畳む (下記) |
| `_CategoryOut` | 削除 — 共有 `CategoryEmbed` を使用 (下記) |
| `ReadyBriefing` | `BriefingDetail` (state `"ready"` → `"briefing"`) |
| `BriefingResponseInvalidError` (errors.py) | `BriefingLlmResponseInvalidError` |

- `_BriefingChapter`: domain VO `BriefingChapter` と同名 + `_`。trends の
  `_RankedMention` 規約 (domain 同名ミラー) と同型。`ChapterOut` を直接 import する
  手書き frontend コードは現状なし (`ReadyBriefing` 経由で消費)。実装時に再確認。
- `_BriefingKeyArticle`: 単純な Out 落とし (`_KeyArticle`) ではなく prefix 付き。
  frontend alias 層が既に `BriefingKeyArticle` という名前を選んで使っていた実績
  (`KeyArticleBlock.tsx` の import) が根拠。
- `_BriefingArticleEmbed`: 命名議論の帰結。
  - Summary 却下 — この repo で `summary` は AI 要約文を指すフィールド語彙
    (`ArticleBrief.summary` / `ReadyBriefing.summary`)。summary フィールドを
    持たない ArticleSummary は積極的に誤解を生む。
  - Detail 却下 — `ArticleDetail` は詳細画面契約の固有名 (investor_take /
    original embed 持ち)。実物より多くを約束し本物と衝突する。
  - Embed 採用 — `schemas/embeds.py`「トップレベルの API レスポンスにはならない。
    常に親レスポンススキーマ内にネストされて利用される軽量スキーマ」の定義
    そのもの。`NewsSourceEmbed` / `CategoryEmbed` / `OriginalArticleEmbed` の前例。
    共有 embeds.py に置かず briefing prefix を付けるのは、keyPoints / url 構成が
    briefing 紙面専用の形のため。
- `BriefingDetail`: "ready" は precondition 型の語彙 (domain の `ReadyForBriefing`
  = 次の処理に進める状態を型で表す、が正しい用法) であり、レスポンスの名前では
  ない。`GET /api/v1/briefing/{categorySlug}` は詳細画面のトップレベル契約なので
  三語彙の Detail (`ArticleDetail` と同型)。trends が bare `Trends` なのは
  list / detail の二面を持たない単一面だからで、一覧契約 (`BriefingListResponse`)
  を併せ持つ briefing には Brief / Detail の語彙が適合する (trends との非対称は
  原理的)。bare `Briefing` は BC 名・domain model (`WeeklyBriefing` の将来
  de-Weekly) と衝突しうるため採らない。discriminator は
  `state: Literal["briefing"]` へ (ライフサイクル語排除、trends の
  state="trends" と同規律、過去合意 `"briefing"|"empty"` の通り)。
  `EmptyBriefing` / `BriefingResponse` は据え置き。
- `BriefingLlmResponseInvalidError`: 同一パッケージ内で "Response" が API
  レスポンス union (`BriefingResponse`) と LLM 応答エラーの 2 つの意味で
  使われていた衝突を解消。このエラーは LLM 応答の schema 不一致 /
  article_ids ハルシネーションを指すため Llm を明示する。API 契約には
  影響しない内部 rename (llm.py / tests が追随対象)。

### keyArticles の形状: lookup 廃止 + 自己完結 nested 化

```python
class _BriefingArticleEmbed(_CamelBase):
    """briefing が参照する記事の埋め込み表現 (読み出し時 join、記事側の現在の事実)。"""
    id: int
    translated_title: str            # title_ja から統一 (/news と同じ契約語彙へ)
    source: NewsSourceEmbed          # source_name: str を置換 (attribution_label が手に入る)
    url: str
    published_at: datetime | None    # 元記事の公開日時のみ null 許容 (記事側の事実)
    key_points: list[str]            # 追加 (InScopeAssessment.key_points 由来)

class _BriefingKeyArticle(_CamelBase):
    """briefing の編集判断 + 参照記事を自己完結で持つ。"""
    significance: str                # 生成時に固定された「なぜ重要か」(JSONB 由来)
    article: _BriefingArticleEmbed   # non-nullable
```

- `ReadyBriefing` から `articles[]` lookup と `_MAX_REFERENCED_ARTICLES` を撤去。
- 外側 `article_id` は削除し `article.id` に一本化する。常に同値の id を 2 つ
  運ばない (出口契約は JSONB 鏡像の義務なし)。React key / 内部リンクは `article.id`。
- F10 max_length 防御は embed の各 str / list に維持する
  (key_points の上限値は実装時に決定)。
- frontend の `title_ja` 二重語彙 (`titleJa` vs `/news` の `translatedTitle`) を解消。
  router は `row.translated_title` を詰め替えていただけで、DB 上も同一データ。

### article non-nullable の根拠 (検証済み)

「分析して重要だと言っているのに参照先が無いはありえない」をシステム側で裏取りした。

- 生成時: domain validator が `key_articles[].article_id ⊆ input_ids`
  (in-scope assessed) を強制。生成時点で記事 + curation + assessment が必ず存在。
- 生成後: 記事の物理削除経路は backend 全体で唯一 `_delete_aged_out_curations`
  (queue/tasks/backfill.py) で、選定クエリ (queue/helpers/backlog.py) は
  `ArticleCuration.id IS NULL` (一度も curation されなかった記事) に限定。
  briefing 参照記事は必ず curation を持つため構造的に対象外。assessment 側の
  age-out は記事を削除しない (sentinel 方式)。source 削除は `Article.source_id`
  FK RESTRICT で連鎖しない。
- この保証は FK ではなく「生成時検証 + 削除経路の不在」による
  (key article の id は JSONB 内にあり FK が張れない)。壊れた場合は FastAPI の
  response 検証が loud に 500 を返す (既存 F10 / failure_visibility 方針と同挙動)。
  **将来 assessed 記事の retention 削除を導入する場合は本不変条件の見直しが必須。**

### watchPoints の形状: wrapper 削除 + `list[str]` 化

```python
watch_points: list[
    Annotated[str, Field(max_length=MAX_WATCH_POINT_STATEMENT_LEN)]
] = Field(max_length=MAX_WATCH_POINTS_PER_BRIEFING)  # F10 ガードは両軸とも維持
```

- `_WatchPointOut` は rename ではなく削除。現在の消費者 (`WatchPoints.tsx`) は
  `wp.statement` しか触っておらず (React key 含む)、`list[str]` で完全に等価。
- object wrapper の存在理由は domain docstring 自身が「段階 2 で
  ``basis_article_ids`` を additive に足せるよう」と将来拡張のみを挙げており、
  Scope Rules (将来の拡張性だけを理由に抽象化しない) に反する。keyArticles
  reshape で ReadyBriefing の破壊的変更は確定済みのため、同梱の限界コストはゼロ。
- **domain VO / JSONB は object 形 (`[{statement}]`) を据え置く。** 既存 DB 行が
  この形で永続化済み (畳むと JSONB migration が必要)、かつ
  `WeeklyBriefingContent` は LLM 出力契約でもあり形変更はプロンプト・検証に
  連鎖する。利得は見た目のみでコストだけが実在する。router が `statement` を
  引き抜く (出口契約は JSONB 鏡像の義務なし)。
- 段階 2 (記事接地) が実際に来た場合: domain は basis_article_ids を additive に
  追加し、response は**その時点で** `[{statement, articles: [...]}]` へ break する
  (「必要になった時点で watch point 側が自分の embed を持つ」方針と整合)。
- frontend: `BriefingWatchPoint` alias を削除し、
  `watchPoints.map((statement, i) => ...)` の直接描画へ。

### category: 専用型を廃止し共有 `CategoryEmbed` を使用

- 判断規則: **feature 専用型が正当化されるのは形が役割固有のときだけ**
  (`_BriefingArticleEmbed` は keyPoints / url が紙面専用なので専用型)。カテゴリは
  BC 横断の共有概念で、briefing 固有の形が無い。「親レスポンスに埋め込む
  カテゴリ参照」のための共有型 `CategoryEmbed` (slug + name) が既にある。
- 唯一の差分 `id` は削れることを検証済み: briefing frontend の `category.id`
  使用は React key 3 箇所のみ (BriefingIndexView / BriefingPendingRow /
  briefing-list page-model) で、一意・安定な slug で 1:1 代替できる。
  `CategoryEmbed` の「id は持たない (表示と絞り込みに不要)」という既定判断とも
  一致する。
- `ReadyBriefing.category` / `EmptyBriefing.category` / `BriefingListItem.category`
  の 3 箇所とも `CategoryEmbed` に置換。frontend の `BriefingCategory` alias は
  削除し React key は slug へ。これで `index.ts` の briefing alias rename 層は
  完全に空になる。

### keyPoints のデータ源

- briefing JSONB には焼かない (assessment 側更新との stale copy を作らない)。
  response 組立時の join で取得する。
- 組立クエリは translated_title のため既に InScopeAssessment を join しており、
  select への列追加のみでクエリ増ゼロ。

### frontend のクリック挙動 (API 契約変更なし)

- key article カードのタイトルクリック = 内部詳細 `/news/[id]` へ遷移。
  外部原文へは別の明示ボタン (ArrowUpRight アイコン流用) に分離。
- `article.id` / `url` とも契約に揃っており frontend のみで完結する。
  articles lookup に居る記事は curation + assessment join 由来のため必ず
  `/news/[id]` で表示可能。briefing / news とも同じ `(protected)` 配下。
- lookup 廃止に伴い `articlesById` Map・props 引き回し・`article &&` ガードを削除。
  significance のみ表示の silent fallback は起こりえない欠損への fallback
  だったため廃止する (failure_visibility)。

### stale docstring の修正 (実装時に同梱)

- `domain/briefing.py` の旧 path 参照 `schemas/briefing.py` (2 箇所) を
  フラット化 (#782) 後の実体 `briefing/schemas.py` へ更新する。
- `schemas.py` 冒頭の「anon GET 経路」を、内部認証 3 層化 (#780) 後の実態
  (`require_bff_request` 保護下の user-less 共有 read) に合わせて更新する。

## 実装時の手順

1. backend: schemas rename + reshape、router の組立変更
   (article summaries 取得を embed 構築に変更 + key_points を select に追加)、
   errors.py の rename (llm.py / tests 追随)、stale docstring 修正
2. `/gen-types` 再生成
3. frontend: BriefingDocument / KeyArticleBlock / ArticleCard / WatchPoints /
   page-model / features/briefing/schemas の追随、`index.ts` の briefing alias
   rename 層削除、state narrowing の追随 (zod の `z.literal("ready")` /
   `Extract<..., { state: "ready" }>` / index.ts の narrowing 交差型 → "briefing")、
   クリック挙動変更
4. `/check`

破壊的変更 (articles[] 撤去 / keyArticles 形状 / watchPoints の list[str] 化 /
category からの id 撤去 / titleJa → translatedTitle / state "ready" → "briefing" /
生成型名の変更) は許容で合意済み。

関連の別スコープ決定: `CategoryDetail` → `CategoryBrief` rename は
[`category-brief-rename.md`](../news/category-brief-rename.md) に分離 (briefing 実装と独立に実行可能)。

## 未決

なし (2026-06-10 に全項目合意済み)。

## 関連して維持する設計 (再 litigate しない)

- **chapters を構造化 (`[{heading, body}]`) のまま返す。** backend で整形済み
  文字列に畳まない。小見出しの表示分けは frontend の表示判断であり、構造を
  文字列に焼くと frontend に parse による復元を強いる + LLM 出力の markup
  injection 面を広げる。旧 `overview` 単一長文を構造化した経緯を逆行しない。
- **domain VO と response schema の二重定義は意図的。** 入口契約 (LLM→DB、
  min_length あり) と出口契約 (DB→frontend、max_length のみ) は検証の意味が
  異なる。上限定数は domain から import して共有しドリフトを構造的に防ぐ。
- **`_BriefingKeyArticle` をフラット化しない。** significance (生成時に固定された
  編集判断) と article.* (読み出しのたび join される記事側の現在の事実) は
  出所と寿命が違い、1 段の nested がその境界を構造として文書化する。
  Embed という実在の概念境界・UI 部品境界 (ArticleCard) とも一致する。
- **articles[] lookup へ戻さない。** dedup 利得ゼロ + 利用側 join 強制が理由。
  watchPoints 段階 2 の記事接地 (basis_article_ids) を理由に lookup を先回りで
  復活させない (将来の拡張性だけを理由にした抽象化の禁止)。必要になった時点で
  watch point 側が自分の embed を持てばよい。
- **watchPoints を response の都合で domain / JSONB まで畳まない。** 永続化済みの
  形 + LLM 出力契約であり、変更は migration とプロンプト連鎖のコストのみで
  利得がない。response の `list[str]` 化は router の引き抜きで完結させる。
