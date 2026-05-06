# C-2: Alpha Vantage News API 統合 + fetch_logs 基盤

2ステップ構成。Step 1 は全ソース共通基盤、Step 2 は AV 固有実装。
変更ファイル: 11（新規 5 + 変更 6）。フロントエンド変更なし。

## Step 1: fetch_logs テーブル + 記録基盤

■ 新規: backend/app/models/fetch_log.py
- FetchLog(SQLModel, table=True): id, source_id(FK news_sources.id CASCADE), status(VARCHAR20 "success"/"error"), articles_count(INT default 0), error_message(TEXT null), duration_ms(INT null), fetched_at(TIMESTAMPTZ default now)
- Relationship: source → NewsSource

■ 変更: backend/app/models/news_source.py
- fetch_logs: list["FetchLog"] = Relationship(back_populates="source") 追加

■ 変更: backend/app/models/__init__.py — FetchLog インポート追加

■ 新規: backend/alembic/versions/a6_create_fetch_logs.py
- fetch_logs テーブル作成 + 複合インデックス (source_id, fetched_at)

■ 変更: backend/app/services/news_fetcher.py
- 各ソースフェッチ完了後に FetchLog を session.add()（成功/失敗問わず）
- commit タイミングは既存と合わせる（ループ内で個別 commit しない。L310 付近の既存一括 commit に含める）
- start_time = time.monotonic() でフェッチ前に計測開始、完了後に duration_ms 算出

■ テスト: backend/tests/test_fetch_logs.py (NEW)

■ 変更: docs/02_DATABASE_DESIGN.md — fetch_logs テーブル追記、ER図更新

## Step 2: Alpha Vantage クライアント + 統合

■ 変更: backend/app/config.py
- av_api_key=""（空=無効）, av_api_base_url, av_topics="technology", av_limit=50, av_max_daily_requests=25

■ 新規: backend/app/services/alpha_vantage.py
- GET https://www.alphavantage.co/query?function=NEWS_SENTIMENT&topics={av_topics}&time_from={YYYYMMDDTHHMM}&sort=LATEST&limit={av_limit}&apikey={av_api_key}
- レスポンスマッピング: feed[].title→title_original, feed[].url→url, feed[].summary→description_original, feed[].time_published→published_at
- time_published パース: 標準形式は YYYYMMDDTHHMMSS（秒あり）。YYYYMMDDTHHMM（秒なし）もフォールバック対応。タイムゾーンは UTC 前提で tzinfo=UTC を付与
- guid形式: "av:{sha256(url)[:16]}"
- フェッチ前にクォータチェック: SELECT COUNT(*) FROM fetch_logs WHERE source_id={av_id} AND fetched_at > 今日0時UTC < av_max_daily_requests
- av_api_key が空文字ならスキップ（エラーではない）
- AV はエラー時も HTTP 200 + {"Information": "..."} を返す → ハンドリング必要
- SourceFetchResult を返す（既存と同じ）

■ 変更: backend/app/services/news_fetcher.py
- api_endpoint == "alpha-vantage" ディスパッチ追加
- av_api_key 未設定時はスキップ

■ テスト: backend/tests/test_alpha_vantage.py (NEW)
■ conftest.py: sample_av_source fixture 追加

■ 重複排除: url UNIQUE + guid UNIQUE で自動処理
■ 後続パイプライン: 変更不要（RSS/HN と同じ経路）
■ シード: NewsSource(name="Alpha Vantage", source_type="api", api_endpoint="alpha-vantage", site_url="https://www.alphavantage.co", fetch_interval_minutes=1440)
