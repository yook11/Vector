# Logfire 統合 — 進行ロードマップ

監査基盤 (`pipeline_events`) とは別の **オペレーショナル可観測性** 層として、
Logfire を Vector に導入するための spec。本ファイルは plan モードで参照する
ことを想定し、コンテキスト消費を最小化するため詳細議論は省き **方針と段階**
だけを書く。

## 1. 監査基盤との責務分担

| 質問の性質 | 答える層 | 保持期間 |
|---|---|---|
| 「source A の parse_failed は何件?」「prompt_version 別失敗率は?」 | 監査 (pipeline_events) | 数ヶ月〜年 |
| 「extraction worker の P99 レイテンシ?」「LLM API エラー率は今?」「このリクエスト今どこで詰まってる?」 | Logfire | 数日〜数週 |
| エラー / 例外 | **両方** (粒度が違う) | — |

境界:
- ドメイン的に意味あるビジネス分析 → 監査
- リクエスト / タスク / 外部 API call / SQL 実行の細部 → Logfire
- 例外は両方に出す

両者は **競合せず補完層**。

## 2. 現状 (2026-05-07)

- `pyproject.toml` に `logfire` パッケージなし
- `app/config.py` / `app/main.py` / `app/broker.py` に Logfire 設定なし
- `app/observability/` 配下に Logfire 統合なし
- `microsoft_research.py:138` のコメントに「logfire で検知」という将来予定の
  記述があるのみ

→ **完全に未導入**、新規導入 PR が必要。

## 3. 進行 Phase

### Phase 1: デフォルト計装の導入 (PR-L1) — 監査基盤と並行可

OpenTelemetry の auto instrument だけ入れる。**カスタム計装はゼロ**。

scope:
1. `uv add logfire`
2. `app/config.py` に Logfire 設定 (token / 環境変数)
3. FastAPI / worker エントリーポイントで `logfire.configure()` + auto instrument
4. デフォルト計装対象: FastAPI, SQLAlchemy, httpx, taskiq (対応してれば)
5. `.env.example` に `LOGFIRE_TOKEN` 追加
6. デプロイで本番に流れ始める

scope 外 (やらない):
- カスタム計装 (span attribute の追加)
- アラート / SLO 設計
- ダッシュボード設計

理由: 本番運用前に「何を観測すべきか」を先回り設計すると、ほぼ見ない
ダッシュボードと請求書だけが残る (典型的な失敗パターン)。

### Phase 2: 監査基盤の完成

PR3-a / PR3-b / PR3-c / PR4 が merge されて全 Stage の監査が稼働。
本番デプロイ → **1〜2 週間運用観察**。

### Phase 3: 本番観察ベースのカスタム計装 (PR-L2 / PR-L3)

観察結果から「足りない情報」「ノイズが多い箇所」を見極めて追加:

- PR-L2: カスタム計装
  - LLM call の token count / 所要時間を span attribute に
  - `prompt_version` / `reason_code` を span attribute に
  - taskiq task の article_id / source_name を span attribute に
- PR-L3: SLO / アラート / ダッシュボード
  - しきい値はカスタム計装の運用データから決定
  - 古い未処理記事の累積監視 (metric gauge) もここに含む

## 4. PR-L1 のチェックリスト (plan モード時に展開)

着手時に確認すること:
- [ ] Logfire の Pydantic plugin (Pydantic 2 と相性) を有効化するか
- [ ] FastAPI middleware の挿入位置 (既存 middleware との順序)
- [ ] taskiq の OTel 対応状況 (公式 instrumentation があるか / コミュニティ
  パッケージか)
- [ ] 本番 / dev / test 環境での Logfire 送信制御 (test では送らない)
- [ ] `LOGFIRE_TOKEN` の secret 管理 (Fly.io secrets / docker compose env)
- [ ] healthcheck endpoint の trace を抑制 (ノイズ削減)

## 5. 監査基盤完成までの参照点

PR3-a 系統が進行中の間、Logfire は **PR-L1 だけ独立で進められる**。互いに
ブロックしないので並行可能。

ただし PR3-a と並行で進めると認知負荷が上がるので、現実的な順序候補:

- **候補 a**: PR3-a-1 → PR3-a-2 → PR-L1 → PR3-b → ...
- **候補 b**: PR3-a-1 → PR-L1 → PR3-a-2 → PR3-b → ...
- **候補 c** (並行): PR3-a-1 と PR-L1 を別ブランチで並行

→ 着手時に判断 (本 spec では決め打ちしない)。

## 6. 残論点 (Phase 3 以降で詰める)

- どの span attribute を焼き付けるか (本番観察ベース)
- アラートしきい値 / 通知先 / SLO 定義
- 古い未処理記事の監視 metric (本 spec §16 → ここに移管予定)
- Logfire とは別の運用ダッシュボード (Grafana 等) を入れるか
