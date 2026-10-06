# トレンドの期間定義と開始条件

## 作業定義

- Problem: 期間計算がwindow・service・schemaへ分散し、Ready構築後にも対象記事の有無を判定している。
- Evidence: domain/window.py、Service.create/execute、schemas.trends_from_snapshot、Briefingの事実取得とfrom_facts、既存の期間・生成済み・競合テスト。
- Invariants: 公開日時基準の完了済み7日間、前の7日間との比較、JST境界、ランキング・要点の選定、APIと保存済みJSON、生成済みと対象0件の正常終了、競合防御、監査・通知の条件を維持する。
- Non-goals: DB構造・権限・migration・スケジュール・分離レベル・Briefing・フロントエンドの変更、本番反映。
- Done: トレンド自身が期間の意味を持ち、ReadyがI/Oなしで開始条件を判定する。単体・結合・専用ロールの実DBテストで従来の業務結果を維持する。

## 責務

- `domain/trend.py`: `TrendWindow`がJSTの終了日を持ち、対象期間・比較期間を導出する。現在日時は呼び出し側から受け取る。`TrendsBundle`はこの期間と全カテゴリの集計結果を持つ。
  - 2026-10-06: [trend-representation.md](trend-representation.md) で、`TrendWindow` を `TrendWeeks`(週と前週)に改め、API の期間を `week` / `previousWeek`(`end` は最終日)にした。
- `domain/ready.py`: 取得済みの事実から「生成済み」「対象記事なし」「開始可能」を判定する。Readyは期間と正の集計元記事数を持ち、DBやログ出力を持たない。
- `repository.py`: 生成済みか、公開期間内の分析済み記事数、カテゴリを取得する。生成済みなら記事数は問い合わせず、未取得をNoneで表す。
- `service.py`: 現在日時の取得、準備、集計、保存、監査・通知を順に行う。executeで事実を取得してReadyを構築し、開始可能な場合だけ_generateへ渡す。準備から保存まで同じセッションを使用するが、分離レベルや入力全体の固定は変更しない。
- `schemas.py`: 期間計算を持たず、トレンドの期間定義からAPI用の開始日・終了日を取り出す。

ReadyはDB上の予約ではない。構築後に別workerが保存した場合も、一意制約とON CONFLICT DO NOTHINGで既存行を維持する。

## 検証

期間はJST境界・任意の曜日・月年またぎ・UTCで渡された現在日時を確認する。Readyは未生成かつ記事ありの場合だけ成立し、生成済みを対象0件より先に判定する。実DBでは公開日時基準の件数、生成済み時の早期終了、準備後の保存競合、専用ロールでの生成と保存済みデータの読み取りを確認する。
