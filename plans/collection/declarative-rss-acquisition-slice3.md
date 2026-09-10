# 宣言的RSS取得 — スライス3実装プラン

Status: 実装・検証完了
作成日: 2026-09-10
仕様: [宣言的なRSSソースと方式別Fetcherへの移行](../../specs/collection/declarative-rss-acquisition.md)
Issue: [#295](https://github.com/yook11/Vector/issues/295)

## Problem / Evidence

NASA・Cornell Chronicleを取得宣言と必要なselect関数へ移し、単一・複数フィードを同じRSS Fetcherで実行する。

基準commitは`69c4953f1`。スライス2の本文ルール・任意関数を確認済み。既存のMultiFeedRssReaderは両ソースだけが利用し、逐次巡回・結果結合・既知の取得失敗の隔離を担う。重複除去は両ソースのselectからdedup_by_linkを呼ぶ。関連既存テスト、保存Service、Taskと監査への接続を確認した。

## Invariants / Non-goals / Done

- URL・宣言順・parse mode・取得頻度・origin・補完方針・FetchedArticleの出力を維持する。
- 正常な空フィードも成功。一部取得失敗はログに記録して続行し、全失敗ならフィードごとの例外と原因をRssFeedErrorsにまとめて既存失敗処理へ渡す。
- 想定外例外・固有関数の例外は握りつぶさず、既存の保存トランザクションを維持する。
- DB・API・依存・再試行・並列化・フィード別設定・DB監査の拡張は行わない。
- 両ソース移行、旧巡回機構撤去、出力・失敗経路の検証、仕様更新と一つのPR作成で完了とする。

## 実装契約

| ソース | feeds | parse mode | 本文 | 固有関数 |
| --- | --- | --- | --- | --- |
| NASA | acquisition.feedsへ6URLを直接記述 | text | CONTENT_ENCODED | select |
| Cornell Chronicle | acquisition.feedsへ6URLを直接記述 | bytes | DISCARD | select |

1. RssFetcherが同じ注入済みRssReaderへfeedsを順番に渡し、全取得結果を結合する。単一フィードも同じ経路を通す。
2. in_scopeの適用後、runtime_checkableなRequiresSelectionで検出したselectを一度呼ぶ。空列にも適用し、未定義時はそのまま通す。
3. select後にURL変換・日時解決・本文採用・平文化・本文変換を行い、FetchedArticleを出力する。
4. RequiresSelectionは`select(entries: list[RssEntry]) -> list[RssEntry]`の呼び出し契約。ソースはProtocolを継承せずstaticmethodを定義する。
5. 両ソースのselectはdedup_by_linkを使用する。非空のraw linkで初出を残し、内容を合成しない。空linkは全件通し、URL変換前に実行する。
6. Reader呼び出しのExternalFetchError・UnreadableResponseErrorだけを捕捉する。404も既存どおり続行する。想定外例外は即時伝播して後続フィードを取得しない。固有関数は捕捉範囲外に置く。
7. source_feed_fetch_failed（source/feed/code/error）とsource_feed_fetched（source/feed/entries_count）の記録を維持し、単一フィードにも適用する。部分失敗のDB監査は追加しない。
8. MultiFeedRssReaderとReaderToolsの専用factory、両ソースのread/map_entry・旧基底継承・endpoint_url、NASA本文helperを撤去する。

## 検証計画

- 巡回順・フィード内順・parse mode・重複通過、部分失敗・全失敗・全空成功・空成功と失敗の混在を検証する。
- 全例外と原因の維持、想定外例外での即時停止、固有関数失敗を取得失敗として捕捉しないことを検証する。
- select未定義・空列・呼び出し順・返却順、初出内容と空linkの維持を検証する。
- 既存fixtureで旧実装と全フィールドを比較し、スライス1・2と未移行経路の回帰も確認する。
- 実DBで部分成功時の保存、全取得失敗・select失敗時の未保存と監査、後続変換失敗時のrollbackを確認する。
- 旧Reader直接呼び出し・NASA本文helper・複数フィード拒否のテストは共通入口の振る舞い検証へ移す。
- checkスキルのlint・format・全単体テスト・専用DB統合テストを実行する。型チェックCLI未構成なら依存を追加せず未実行理由を記録する。

## 作業環境と成果物

インフラ作業とは別の`/Users/yook1/Vector-rss-slice3` worktree、`refactor/declarative-rss-slice3`ブランチを使用する。既存のインフラ作業ツリーは変更しない。仕様と本プランを更新し、日本語のコミット・PRを作成する。Issue全体はスライス4まで完了にしない。

## 実装・検証結果

- NASA・Cornellを宣言とstaticmethod selectへ移行し、旧巡回Readerと専用factoryを撤去した。
- Reader・converter・Service・失敗handler・DBモデルの処理は変更していない。
- ソース別fixtureと共通入口からのテストへ移し、暫定の複数フィード拒否テストを削除した。
- 取得・ソース関連は595 passed。全backend単体は6306 passed（`-m 'not integration'`）。
- 初回実装時は最初の例外の伝達を確認した。追加変更では全例外の伝達を確認する契約へ置き換える。
- backend全体のruff lint・formatと変更テストのlint・formatは通過。
- 基準commitの旧Reader・旧ソースと新取得経路を同じ7fixture（下表の4組）で実行し、全FetchedArticleフィールドが一致した。外部へのlive取得は行っていない。
- 専用backend型チェックCLI・設定は未構成のため静的型チェック未実行。依存追加なし。

| ソース | 比較したfixture |
| --- | --- |
| NASA | nasa_rss.xml |
| NASA | nasa_for_oracle_feed_a.xml / nasa_for_oracle_feed_b.xml |
| Cornell | cornell_rss.xml / cornell_rss_health.xml |
| Cornell | cornell_for_oracle_feed_a.xml / cornell_for_oracle_feed_b.xml |

`make test-integration PYTEST_ARGS='-x -q -rs'`: **1354 passed、22 skipped**。追加した部分成功の保存・全取得失敗とselect失敗の監査・複数フィード取得後の変換失敗rollbackを含む。専用Docker project `vector-test-vector-rss-slice3-423720308`のPostgres・Redisは終了後に削除済み。

22件のskipは既存の`tests/test_db_user_isolation.py`で、Alembic適用済み`public.watchlist_entries`を要求する条件による。


### 宣言の読みやすさの調整

フィード一覧は別定数へ分離せず、各ソースのacquisition.feedsへ直接記述する。テストも宣言を参照する。first_errorとsuccess_countを廃止し、失敗一覧の件数で全フィード失敗を判定する。


### 追加合意：RSS失敗一覧（2026-09-10）

- RssFeedFailureはfeed_urlと元のExternalFetchError / UnreadableResponseErrorを保持する。
- RssFeedErrorsはAcquisitionErrorを継承し、空でない失敗一覧をtupleで保持する。型名に全件失敗の条件を含めず、投げる条件はFetcherが所有する。
- 単一フィードを含め全取得失敗時は、巡回順の全原因をまとめて伝播する。部分成功時は既存どおり結果を採用し、想定外例外と固有関数は集約しない。
- 全体のcodeはrss_feed_errors、failure_kindはrss_feeds。個別原因の既存分類を使い、一つでもretryableなら全体もretryableとする。スケジュール・自動再試行回数は変更しない。
- 監査payloadへ任意のfeed_failuresを追加し、各URL・原因code・例外クラス・安全なメッセージ・原因チェーンを保存する。URLは既存の秘匿処理を通し、例外の生メッセージは保存しない。
- 集約例外の文字列表現は件数のみ。既存の単一原因AcquisitionReadErrorは未移行経路向けに維持する。
- DBカラム・API schemaは変更しない。OpenAPIの変更前後一致を確認したためfrontend型生成は不要。

追加変更の関連テストは607 passed、全backend単体は6314 passed。backend全体と変更テストのruff lint・formatは通過。OpenAPIを変更前後で比較して一致を確認した。専用backend型チェックCLIは引き続き未構成のため未実行。

追加変更の`make test-integration PYTEST_ARGS='-x -q -rs'`は1355 passed、22 skipped。複数原因の監査保存と混在時の再試行分類を実DBで確認した。skip条件は上記と同じ既存の権限境界テスト。一時Postgres・Redisは終了後に削除済み。
