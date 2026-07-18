# 管理画面: ニュースソース状態確認

作成日: 2026-06-09
Status: Implemented (PR #778)

## 目的

管理者がニュースソースごとの取得状態と分析可能化の状況を確認し、問題のある
ソースを素早く特定できるようにする。

## 非対象

- 新規ニュースソース登録 UI の拡張
- ソース編集機能
- 手動 fetch 実行
- カスタム期間指定
- グラフ表示
- 詳細ログ画面

## 対象画面

管理画面にニュースソース状態確認ページを追加する。

表示窓は切り替え可能にする。

- 初期値: `24h`
- 選択肢: `24h` / `48h` / `72h` / `7d`

上部に表示窓の切り替えを置き、その下に source ごとの状態一覧を表示する。

一覧の 1 行は、次の情報で構成する。

| 表示グループ | 内容 |
|---|---|
| Source info | source name / type / active をひとまとまりで表示する |
| Analyzable | analyzable rate と analyzable count / processed article count を表示する |
| Incomplete | 現在の incomplete count を表示する |
| Failure reasons | 選択期間内の outcome code 別集計を表示する |
| Last succeeded | 直近成功時刻を表示する |

`source name` / `type` / `active` は別々の指標ではなく、その source を識別する
基本情報として扱う。

## API 契約

```text
GET /api/v1/admin/sources/health?windowHours=24
```

`windowHours` の許可値:

- `24`
- `48`
- `72`
- `168`

未指定時は `24` とする。

レスポンス概要:

```ts
{
  windowHours: 24,
  observedAt: string,
  items: [
    {
      sourceId: number,
      sourceName: string,
      sourceType: "rss" | "api" | "html",
      isActive: boolean,
      analyzableRate: number | null,
      analyzableCount: number,
      processedArticleCount: number,
      incompleteCount: number,
      failureReasons: [
        {
          outcomeCode: string,
          count: number
        }
      ],
      lastSucceededAt: string | null
    }
  ]
}
```

並び順:

- `items`: `sourceName` 昇順
- `failureReasons`: `count` 降順、同数の場合は `outcomeCode` 昇順

返さない情報:

- `siteUrl`
- `endpointUrl`
- free-text error message
- payload の詳細

## 不変条件

この章は実装とテストで固定する振る舞いの正本とする。

### 指標の不変条件

#### Analyzable count

`analyzable count` は、選択窓内に分析可能な記事として保存された件数を示す。

次の合計値である。

| stage | event type | outcome code | 意味 |
|---|---|---|---|
| `acquisition` | `succeeded` | `article_created` | Stage 1 で即時に分析可能記事として保存された件数 |
| `completion` | `succeeded` | `article_completed` | Stage 2 の補完工程を経て分析可能記事として保存された件数 |

Stage 1 の即時保存だけ、または Stage 2 の補完保存だけを片方だけ集計してはならない。

#### Processed article count

`processed article count` は、選択窓内に処理した記事候補のうち、本画面の
`analyzable rate` の母数として扱う総数を示す。

次の合計値である。

```text
processed article count
  = analyzable count
  + acquisition/rejected/*
  + completion/rejected/*
```

`rejected` は、候補を処理したうえで分析可能化できないことが判定された結果として扱う。

`failed` は記事候補単位とは限らないため、`processed article count` に含めない。

`incomplete_article_created` は未確定の途中状態であるため、`processed article count`
に含めない。

#### Analyzable rate

`analyzable rate` は、処理した記事候補のうち分析可能化できた割合を示す。

```text
analyzable rate = analyzable count / processed article count * 100
```

`processed article count = 0` の場合、API は `analyzableRate: null` を返す。

画面では `analyzableRate: null` を `-` と表示する。

#### Incomplete count

`incomplete count` は、現在まだ補完待ちまたは補完実行中の件数を示す。

表示窓には依存しない現在値である。

対象 status:

| status | 扱い |
|---|---|
| `open` | 数える |
| `running` | 数える |
| `closed` | 数えない |

`incomplete_article_created` は、rate の母数ではなく `incomplete count` の現在値として
観測する。

#### Failure reasons

`failure reasons` は、選択窓内に失敗・棄却として観測された理由別件数を示す。

対象 event:

| stage | event type |
|---|---|
| `acquisition` | `failed` |
| `acquisition` | `rejected` |
| `completion` | `failed` |
| `completion` | `rejected` |

outcome code ごとに集計する。

`failed` は `processed article count` には含めないが、`failure reasons` には含める。

`rejected` は `processed article count` と `failure reasons` の両方に反映する。

選択窓内に存在する outcome code はすべて確認できるようにする。

並び順は `count` 降順、同数の場合は `outcomeCode` 昇順とする。

表示領域に収まらない場合も、集計結果自体は省略しない。

#### Last succeeded at

`last succeeded at` は、その source が直近に分析可能記事を生んだ成功時刻を示す。

表示窓には依存しない。

`analyzable count` と同じ「分析可能記事を生んだ成功」だけを対象にする。

| stage | event type | outcome code |
|---|---|---|
| `acquisition` | `succeeded` | `article_created` |
| `completion` | `succeeded` | `article_completed` |

対象 event の最大 `occurred_at` を返す。`incomplete_article_created` のような
分析可能化に至らない成功は含めない。対象成功がない場合は `null` を返す。

### 表示窓の不変条件

- 初期表示は `24h` とする。
- 選択可能な表示窓は `24h` / `48h` / `72h` / `7d` に限定する。
- API の `windowHours` は `24` / `48` / `72` / `168` のみ許可する。
- 選択窓を使う指標は、window start 以降の event を対象にする。
- `incomplete count` と `last succeeded at` は表示窓に依存しない。

### ソース一覧の不変条件

- 全ニュースソースを表示する。
- inactive source も表示する。
- 選択窓内に event がない source も表示する。
- source は `sourceName` 昇順で安定して並べる。
- event がない source は count `0`、`analyzableRate: null`、`failureReasons: []`、
  `lastSucceededAt: null` を返す。

### データ最小化の不変条件

- API response に `siteUrl` を含めない。
- API response に `endpointUrl` を含めない。
- API response に free-text error message を含めない。
- API response に `pipeline_events.payload` の詳細を含めない。

## テストを書くときの注意点

テストは上記の不変条件が崩れないことを確認する。

- `article_created` と `article_completed` を同じ source に混ぜ、`analyzable count`
  が両方の合計になることを確認する。
- `rejected` が `processed article count` に反映されることを確認する。
- `failed` が `processed article count` に反映されず、`failure reasons` に反映される
  ことを確認する。
- `incomplete_article_created` が `processed article count` に反映されないことを
  確認する。
- `open` / `running` / `closed` を混ぜ、`incomplete count` が `open + running`
  だけになることを確認する。
- outcome code が複数ある場合、全件返り、`count` 降順、同数なら `outcomeCode`
  昇順になることを確認する。
- window start ちょうどの event は含まれ、それより前の event は除外されることを
  確認する。
- active source / inactive source / event がない source を混ぜ、全 source が
  一覧に出ることを確認する。
- API response に不要な source 詳細や error message が含まれないことを確認する。
- frontend は `analyzableRate: null` を `-` 表示にすることを確認する。
- frontend は failure reasons が多い場合でも全件確認できることを確認する。

## 完了条件

管理者が一覧画面で、各ニュースソースの稼働状態、分析可能化率、未完成件数、
主な失敗理由、直近成功時刻を確認できること。
