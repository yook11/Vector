C-1: Hacker News API 統合

■ 新規: backend/app/services/hacker_news.py
- Algolia HN Search API (GET https://hn.algolia.com/api/v1/search_by_date)
- params: tags=story, numericFilters=points>{settings.hn_min_points},created_at_i>{last_fetched_timestamp}, hitsPerPage={settings.hn_hits_per_page}
- url が null の hit はスキップ（Ask HN等）
- guid形式: "hn:{objectID}"
- SourceFetchResult を返す（既存と同じ）
- レスポンスマッピング: hits[].title→title_original, hits[].url→url, hits[].objectID→guid, hits[].created_at→published_at, hits[].created_at_i→次回取得用timestamp

■ 変更: backend/app/services/news_fetcher.py L265-278
- else ブランチを api_endpoint でディスパッチ
- "hacker-news" → HackerNewsClient.fetch_and_save_stories()

■ 変更: backend/app/config.py
- hn_api_base_url, hn_min_points=20, hn_hits_per_page=50

■ テスト: backend/tests/test_hacker_news.py (NEW)
■ conftest.py: sample_hn_source fixture追加

■ 重複排除: url UNIQUE + guid UNIQUE で自動処理
■ 後続パイプライン: 変更不要（RSS と同じ経路）
■ シード: NewsSource(name="Hacker News", source_type="api", api_endpoint="hacker-news", site_url="https://news.ycombinator.com", fetch_interval_minutes=360)