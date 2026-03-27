# パイプライン改善: 将来の課題

Step 2（タスク分割）のスコープ外で、将来取り組む課題をまとめる。

## Intelligence 層（意味的グルーピング）

現行の `detect_duplicates` は embedding 間コサイン距離をログ出力するのみ。
将来的には「関連付け」として設計し直す:

- 「意味的に似ている記事は重複ではない。全件分析すべき」
- 同じトピックでも情報源ごとに切り口・センチメント・強調点が異なる
- グルーピングは「排除」ではなく「関連付け」。canonical + N sources バッジで表示

### 必要な作業
- DB スキーマ: グループテーブルの設計
- API: グルーピング結果の取得エンドポイント
- UI: canonical + sources バッジの表示
- タスク: 定期バッチタスク（per-article チェーンとは独立したスケジュール）

## RobotsCache の共有化

`fetch_content` タスクは httpx.AsyncClient / RobotsCache を都度生成している。
1タスク1HTTPリクエストなので現在は問題ないが、記事数増加時に同一ドメインの
robots.txt を重複取得する。

### 将来の対応
- Redis キャッシュ（`robots:{domain}` → allowed/blocked, TTL 1h）で共有化
- タスク単位の設計は維持

## skip_content_fetch の理由記録

現在は構造化ログ（structlog の reason フィールド）で記録。
UI に「取得失敗の理由」を表示する要件が発生した場合:

### 将来の対応
- `skip_content_reason: str` カラムの追加
- `"http_403"`, `"robots_blocked"`, `"max_retries_exhausted"`, `"quality_gate"` 等

## extract_contents（バッチ関数）の削除

Step 2 で `taskiq_worker.py` を廃止した結���、`extract_contents()`（バッチ関数）の
呼び出し元がなくなった。`_fetch_one` / `DomainRateLimiter` 等の関連コードと合わせて
将来削除する。
