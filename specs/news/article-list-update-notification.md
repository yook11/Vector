# 開いている記事一覧の新着検知・更新

Status: Implemented

関連: [Issue #297](https://github.com/yook11/Vector/issues/297)、[PR #296](https://github.com/yook11/Vector/pull/296)

## Problem

記事保存後に Next.js の一覧キャッシュを無効化しても、開いているブラウザは新着を検知しない。既存の保存後通知を更新識別子へ反映し、ブラウザが軽量に確認して、ユーザー操作で一覧を更新できるようにする。

## Evidence

- `backend/app/queue/tasks/assessment.py` は、分析結果の保存成功後に `articles:list` と `articles:categories` を通知する。
- `frontend/src/app/api/internal/revalidate/route.ts` は、認証・入力検証後に `revalidateTag(tag, { expire: 0 })` を実行する。
- `frontend/src/features/news/api/get-articles.ts` と `get-categories.ts` は、`use cache` と `cacheLife("minutes")` で一覧・カテゴリー件数をキャッシュする。
- `frontend/src/app/(public)/page.tsx` は、一覧・カテゴリー・ウォッチ状態を取得して Server Components で描画する。
- `PaperNewsResultSummary` はページとは別に `getArticles` を呼んでおり、同じ一覧結果を共有できていない。
- 一覧取得失敗には `frontend/src/app/(public)/error.tsx` の既存エラー画面と再試行を利用できる。
- 本番 frontend は通常1台で、`node server.js` により起動する。デプロイ中は新旧タスクが並走する。
- Next.js の `use cache` は関数引数をキャッシュキーに含め、`router.refresh()` は現在のルートを再取得するがサーバーキャッシュ自体は無効化しない。

## Invariants

1. 更新識別子は全カテゴリー共通の不透明なランダム文字列であり、記事ID・件数・ユーザー情報・認証情報を含めない。
2. 初期化時と `articles:list` の正常な通知後に識別子を生成し、型付き `globalThis` 領域を介して同じ Node プロセスの Route Handler と Server Components で共有する。
3. 既存の通知認証・入力検証・タグ無効化を維持し、全タグの無効化成功後に限り識別子を1回変更する。
4. 一覧取得前に識別子を1回読み、同一画面の一覧・総件数・カテゴリー取得へ同じ値を明示的に渡す。一覧は同じ Promise を共有する。
5. 取得開始後の通知は取得結果へ後付けせず、次回確認で検知する。
6. 確認APIは Node 上で識別子のみを `no-store` で返し、backend・記事DBを呼ばない。
7. 表示中だけ60秒間隔で最大1件を確認し、初回・表示復帰時にも確認する。タイムアウト・中断・古い応答を扱い、失敗時は一覧を維持する。
8. 相違検知後は確認を止め、ボタンの二重実行を防ぐ。更新完了は `useTransition` で観測し、識別子が同じでも待機状態を解除する。サーバーから届いた表示識別子を基準として確認を再開する。
9. 記事取得と描画は Server Components に維持し、カテゴリー・並び順・ページ・表示件数は URL のまま維持する。

## Non-goals

- DBの件数・最大記事ID・公開日時を使う検知、DB schema・FastAPI APIの変更。
- SSE、WebSocket、共有Redis、新規依存、認証・認可、backend、DB、インフラの変更。
- カテゴリー別通知、新着件数、先頭ページへの移動、旧画面保持。
- 複数プロセス間の共有、通知の永続化・再送・重複排除、本番デプロイ。

## Done

- 通知から識別子変更、案内表示、ボタンによる条件を維持した一覧更新まで実装される。
- 不正・無関係・失敗通知では識別子が変わらず、確認の反復では記事APIアクセスが増えない。
- Node.js 24で frontend の Biome・TypeScript・Vitest、実Next.jsキャッシュ検証、対象ブラウザテストが成功する。
- 通常の新着通知成功ログ、確認API、表示中ブラウザの案内と更新を本番反映時に確認する手順を記録する。

## 合意した画面動作

- 新着通知は全カテゴリー共通で、初回表示時・60秒ごと・非表示からの復帰時に識別子を確認する。
- 非表示中、一覧以外、新着検知後は確認を止める。
- 相違時は「新しい記事が追加されました」と「一覧を更新」を表示し、件数は表示しない。
- クリック時は同じTransition内で通知を閉じる更新と `router.refresh()` を実行し、現在のURL条件を維持する。更新中はボタンを無効にし、一覧の更新が完了してから通知を閉じる。
- 確認失敗では現在の一覧を維持し、手動更新失敗では既存エラー画面を使う。
- 再起動やデプロイ並走による余分な案内は許容する。

## 実装計画

1. process内識別子をserver-onlyモジュールへ置き、既存通知と読み取り専用確認APIへ接続する。
2. request-timeに確定した識別子を一覧・カテゴリーのキャッシュキーへ含め、同じ一覧Promiseを全表示箇所で共有する。
3. 表示状態・タイムアウト・中断・古い応答・手動更新を扱うClient Componentを追加する。
4. 単体・コンポーネント・実Next.jsキャッシュ・実ブラウザで境界条件を検証する。

## Implementation

process内のランダム識別子、通知・確認API、識別子を含む一覧キャッシュ、一覧Promise共有、表示中ポーリングと手動更新を実装した。変更範囲は frontend、関連テスト・検証スクリプト、本仕様書に限定し、backend・DB・インフラ・依存・認証方式は変更していない。

## Verification

- Node.js 24.19.0で `npx biome check src/`、`npx tsc --noEmit`、`npm test`（134 files / 1472 tests）が成功した。
- `RUN_ARTICLE_UPDATE_BROWSER_TEST=1 node --test scripts/test-article-cache.mjs` が成功した。実Next.js 16.3.4のproduction buildとstandalone起動で、識別子共有・通知前後のキャッシュ・10回の確認で記事APIアクセスが増えないこと・条件別キャッシュ・プロセス再起動を確認した。
- Chromiumで、新着検知から一覧・カテゴリー件数更新と確認再開、同じ識別子での更新完了、二重クリック防止、取得中の追加通知、確認失敗後の復帰、一覧取得失敗と再試行、画面遷移中の確認停止と戻った後の検知、URL条件維持を確認した。
- 非表示・復帰、60秒間隔、タイムアウトと再試行、中断済みの古い応答の無視はコンポーネントテストで確認した。
- ブラウザ検証は、本番の通知・キャッシュ・確認コンポーネント・公開エラー画面をコピーしたstandalone fixtureを使用する。外部API・認証依存とエラー画面の装飾部品はローカルのstubに置き換える。CIの標準実行はキャッシュ検証までで、Chromiumを使う検証は上記の環境変数で明示的に実行する。
- 再試行検証で、Next.js 16.3の `retry` と既存画面の `unstable_retry` が一致していない問題を発見した。今回使う公開一覧の `error.tsx` で `retry` を受け取り、共通表示部品へ渡すよう修正した。他ルートのエラー画面は今回の対象外とする。
- 本番デプロイ・実通知到達は今回実施していない。

## 本番反映時の確認手順

1. frontendが通常構成の1タスクで稼働していることを確認する。
2. 記事保存後の既存revalidate成功ログで `articles:list` と `articles:categories` を確認する。
3. `/api/news/revision` の識別子が通知前後で変わり、レスポンスが `no-store` であることを確認する。
4. 表示中ブラウザで案内が出ること、ボタン押下後にURL条件を維持して新着とカテゴリー件数が反映されることを確認する。
5. 本番デプロイと実通知到達はローカルの実装・検証完了とは別の運用確認として記録する。
