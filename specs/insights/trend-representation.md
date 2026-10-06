# トレンドの表し方

Status: Implemented
工程: backend(domain → schemas) → /gen-types → frontend → /check。保存済みスナップショットの削除は別 PR の contract migration

## 作業定義

- Problem: トレンドの API と domain で、何をトレンドと呼ぶかが決まっていない。名前1件分の型は置かれた順位表の名前 (`RankedMention`) で呼ばれ、どの名前かとその週の値が平らに混ざり、同じ名前が2つの順位表に重複して載る。期間は同じものを `window` と `current` の2つの名前で呼び、比較する期間は名前から読めない。カテゴリは #541 の `Category` の形になっていない。
- Evidence: `insights/trend_discovery/domain/trend.py`・`domain/mention_context.py`・`schemas.py`・`service.py`・`repository.py`・`router.py`、`models/trends_snapshot.py`(bundle は公開レスポンスそのもの)、`frontend/src/features/trends/`、`specs/insights/trend-publication-selection.md`、`specs/insights/trend-domain-organization.md`、`specs/news/category-schema-naming.md`。
- Invariants:
  - スナップショットには公開レスポンスをそのまま保存し、読むときは公開の型で検証してそのまま返す。
  - 集計の対象 (公開日時基準、JST 境界の完了済み7日間とその前の7日間)、候補の条件、伸び率の式と伸び率で並べる条件、要点と一緒に語られた名前の選び方は変えない。
  - 応答はユーザーに依存しない。BFF 経由の証明は今のまま。
  - DB の列 (`window_end`・`source_analysis_count` など) と監査の記録の項目名・値は変えない。
- Non-goals: 集計方法・期間の長さの変更、履歴の表示、要点にどの記事かを付けること、DB の構造、監査の記録、ブリーフィング。
- Done: 下の形で API・domain・画面が揃い、古い形のスナップショットが残っていない。

## トレンドの定義

ある期間に、ある名前がどれだけ・どう語られたか。前の期間からどう変わったか。

階層は「期間 → カテゴリ → 名前」で、どの階層もその範囲のトレンドを表す。一緒に語られた名前 (`CoMention`) はトレンドではなく、名前のトレンドに付く情報。

## 期間

- 週 (`week`): 集計する7日間。スナップショットを作った日 (JST) の前日までの7日間で、暦の週 (月〜日) ではない。スナップショットは毎日作るので、週も1日ずつずれる。
- 前週 (`previousWeek`): 週の直前の7日間。伸び率の比較に使う。
- 修飾のない値 (記事数・要点・一緒に語られた名前) は週のもの。前週の値だけに `previousWeek` を付ける。
- API の日付範囲は両端を含む (`end` は最終日)。DB の `window_end` (週の翌日 = 半開区間の終わり) は変えず、API に出すときに最終日へ直す。

期間の長さや比較の対象を変える要件が出たら、既存のフィールドを付け替えずに足す (保存済みの週のスナップショットは週のまま正しい)。

## API

`GET /api/v1/trends` のパスと認可は変えない。応答は `Trends | null`。

「無い」は2種類あり、表し方を分ける。

- トレンド自体が無い (スナップショットが1件も無い): 200 で `null`。故障ではないため 404 にしない。`EmptyTrends` と目印の `state` は廃止する (中身の無い型を分ける理由がなく、`null` で足りる)。画面は「トレンドはまだ生成されていません」を出す。
- カテゴリのトレンドが無い (集計したが5記事以上の名前が無い): そのカテゴリを載せたまま `mentionTrends: []`。「集計して0件だった」という週の事実なので、カテゴリごと省かない。

記事が1件も無い日は生成を飛ばすので、そのときは前の日のスナップショットが返る (どの週かは `week` で分かる)。

```
Trends
  week: DateRange
  previousWeek: DateRange
  generatedAt: datetime
  analyzedArticleCount: int        週に公開された分析済み記事の数 (全カテゴリ、要点の有無を問わない)
  categoryTrends: CategoryTrends[]

DateRange
  start: date
  end: date                        最終日 (含む)

CategoryTrends
  category: Category               {slug, name}
  mentionTrends: MentionTrend[]

MentionTrend
  name: MentionName
  type: MentionType
  articleVolume: MentionArticleVolume
  growth: MentionGrowth
  keyPoints: string[]
  mentionedWith: CoMention[]

MentionArticleVolume
  count: int                       週にその名前が要点に出てきた記事の数
  previousWeekCount: int           前週の同じ数
  rank: int

MentionGrowth
  rate: float
  rank: int | null

CoMention
  name: MentionName
  type: MentionType
  sharedArticleCount: int
```

| 変更前 | 変更後 |
|---|---|
| `windowStart` / `windowEnd` (翌日) | `week.start` / `week.end` (最終日)、`previousWeek` を追加 |
| `sourceAnalysisCount` | `analyzedArticleCount` |
| `categoryId` / `categorySlug` / `categoryName` | `category: Category` (`categoryId` は公開しない) |
| `mostMentioned` / `fastestGrowing` (`RankedMention[]`) | `mentionTrends: MentionTrend[]` と各名前の順位 |
| `appearanceCount` / `previousAppearanceCount` | `articleVolume.count` / `articleVolume.previousWeekCount` |
| `growthRate` | `growth.rate` |
| `relatedMentions` (`RelatedMention[]`) | `mentionedWith` (`CoMention[]`) |
| `Trends \| EmptyTrends` (`state: "trends" / "empty"`) | `Trends \| null` (`state` は廃止) |

## 順位と公開する名前

- 候補: そのカテゴリで、週に5記事以上に出てきた名前 (今と同じ)。
- 記事数の順位 `articleVolume.rank`: 候補の中で、週の記事数が自分より多い名前の数 + 1。同じ記事数なら同じ順位 (1, 2, 2, 4)。候補には必ず付く。
- 伸び率 `growth.rate`: `(count − previousWeekCount) / max(previousWeekCount, 2)`。前週が少ないときに値が跳ねすぎないよう補正している。すべての名前が持つ。
- 伸び率の順位 `growth.rank`: 伸び率で並べる条件 (前週2記事以上、または週10記事以上) を満たす候補の中で、伸び率が自分より高い名前の数 + 1。同じ伸び率なら同じ順位。条件を満たさない名前は `null`。
- 公開する名前: どちらかの順位が5以内の名前をすべて載せる。5位に同点があれば5件より多くなる。
- `mentionTrends` の並び: 記事数の順位、同じ順位は名前の照合キーの順。
- 要点と一緒に語られた名前は、公開する名前にだけ付ける (選び方は今と同じ)。

## 画面

- 記事数の列は `articleVolume.rank` が5以内の名前を、伸び率の列は `growth.rank` が5以内の名前を、順位の順・同じ順位は名前順に並べる。順位の数字は `rank` をそのまま出す。
- 列の説明「出現回数順」を「記事数順」にする。
- 期間は `week.start – week.end` (最終日) で出す。
- 「新登場」は `previousWeekCount` が 0 のとき (今と同じ)。
- カテゴリの React key は `category.slug`。

## domain

公開の型と同じ概念には同じ名前を使う。

| 変更前 | 変更後 |
|---|---|
| `RankedMention` | 候補 `MentionCandidate` (`name, type, count, previous_week_count`。伸び率と、伸び率で順位を付けるかを持つ) と、順位を付けた `MentionTrend` (記事数と伸びを値と順位のまとまりで持つ) |
| `appearance_count` / `previous_appearance_count` | 記事数のまとまりの `count` / `previous_week_count` |
| `hotness_score` / `is_hot` | 伸びのまとまりの `rate` / `MentionCandidate.is_growth_ranked` |
| `select_most_mentioned` / `select_fastest_growing` | `rank_mention_trends` (候補全体に順位を付け、公開する名前を返す) |
| `MIN_CURRENT` / `MIN_PREVIOUS` | `MIN_CANDIDATE_COUNT` / `MIN_PREVIOUS_WEEK_COUNT` |
| `RelatedMention` / `related_mentions` / `select_related_mentions` / `MAX_RELATED_MENTIONS` | `CoMention` / `mentioned_with` / `select_co_mentions` / `MAX_CO_MENTIONS` |
| `CategoryTrends(category_id, category_slug, category_name, most_mentioned, fastest_growing)` | `CategoryTrends(category_slug, category_name, mention_trends)` |
| `TrendWindow` (`window_end` / `window_start` / `current_*` / `previous_start`) | `TrendWeeks(snapshot_date)` の `week` / `previous_week` (`Week(start, end)`、`end` は最終日) |

- domain のカテゴリは `category_slug` / `category_name` のまま持ち、公開時に `Category` へ変換する。domain は API の schemas に依存しない (briefing と同じ)。
- `Week` は日付の範囲と、クエリに使う公開日時の範囲 (`published_from` 以上 `published_before` 未満、UTC) を持つ。
- `TrendsBundle` は名前を変えない。生成1回分の結果で、公開の `Trends` はこれに生成時刻と記事数を足したもの。
- 保存側の名前 (DB の `window_end` / `source_analysis_count`、監査とログの `window_start` / `window_end` / `source_analysis_count`) は変えず、読み書きする箇所で対応づける。`window_end` は `snapshot_date`、`window_start` は `week.start` に当たり、監査の値も今と同じにする。

## 保存済みのスナップショット

- 古いスナップショットは、上位5に入らなかった順位を持たないため、新しい形に変換できない。全件を削除し、毎日 00:05 JST の生成で作り直す。
- API は最新の1件だけを返し、履歴はどこでも使っていない。
- 削除は contract migration で行い、アプリとは別の PR にする。

## 反映の順番

古いアプリはスナップショットが無いときに空の表示を返すので、削除を先に適用してからアプリを反映する。

1. 削除の contract PR を merge し、DB migration を承認する。古いアプリのトレンドのページは空の表示になる。
2. アプリを本番に反映する。トレンドのページは「トレンドはまだ生成されていません」になる。
3. 次の 00:05 JST に新しい形のスナップショットが作られる。

- 1 と 2 は同じ日の 00:05 JST より前に終える。間に 00:05 JST を挟むと、古いアプリが古い形のスナップショットを作り、2 の後にトレンドのページが 500 になる。その日の分は生成済みとして扱われるので、翌日の生成まで直らない。
- 1〜3 の空表示 (最大で約1日) は許容する。

## 関連する spec

実装の PR で、`trend-domain-organization.md` (`TrendWindow` の責務) と `trend-publication-selection.md` (現在期間・`sourceAnalysisCount`) に、この spec で名前と形が変わった旨を注記する。

## 受け入れ条件

- [x] OpenAPI に上の型が出て、`RankedMention` / `RelatedMention` / `EmptyTrends` / `state` / `mostMentioned` / `fastestGrowing` / `windowStart` / `windowEnd` / `sourceAnalysisCount` が残っていない
- [x] 同点が同じ順位になり、5位の同点がすべて載る。伸び率の条件を満たさない名前の `growth.rank` は null (domain 試験)
- [x] `week.end` が最終日、`previousWeek` が週の直前の7日間 (試験)
- [x] 専用ロールで生成・保存したスナップショットを API がそのまま返す (実 DB 試験)
- [x] 画面の2列が順位から組み立てられ、同じ順位は名前順になる (コンポーネント試験)
- [x] contract migration でスナップショットが全件消え、API が `null` を返す
- [x] `/check` pass
