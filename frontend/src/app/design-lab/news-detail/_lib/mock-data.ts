export const MOCK_ARTICLE = {
  translatedTitle: "Honker、SQLiteにPostgres風の通知/リスニング機能を追加",
  originalTitle: "Honker: Postgres-Style Notify/Listen for SQLite",
  sourceName: "Hacker News",
  author: "Honker",
  publishedAt: "2026-04-30T17:00:00Z",
  analyzedAt: "2026-04-30T17:00:00Z",
  url: "https://example.com/honker",
  // 段落区切り (\n\n) でリズムを作る。将来的に AI 出力側で paragraph break を返すように
  // なれば、そのまま透過できる構造。
  summary: [
    "Honkerは、SQLiteにPostgresスタイルのNOTIFY/LISTENセマンティクス、永続的なパブリッシュ/サブスクライブ、タスクキュー、イベントストリーム機能を追加する拡張機能です。クライアントポーリングやデーモン/ブローカーなしで動作し、クロスプロセスウェイクレイテンシはMシリーズラップトップで約0.7ms (p50) です。",
    "プレーンなSQLiteロード可能拡張機能として提供され、Python、Node、Rust、Go、Ruby、Bun、Elixirなどの言語から利用できます。SQLiteをプライマリデータストアとして使用する場合、キューを同じファイル内に配置することで、ビジネスロジックのトランザクションとキューのコミットを単一トランザクションで実行できます。",
    "HonkerはSQLiteのPRAGMA data_versionを1ミリ秒ごとにポーリングし、コミットのたびにインクリメントされるモノトニックカウンターを利用して通知信号を生成します。これにより、リスナー数の増加に対して無料でスケールします。",
    "pg_notify、Huey、pg-boss、Obanなどの既存ソリューションと比較して、HonkerはSQLite内でACIDトランザクションと統合されたキュー機能を提供する点が特徴です。",
  ].join("\n\n"),
  investorTake: [
    "HonkerはSQLiteにPostgresライクな通知・キュー機能をもたらす軽量拡張であり、データベースとメッセージキューを同一トランザクションで扱える点が際立つ。",
    "分散ブローカー不要でACID性を保ちつつクロスプロセス通知を実現しており、エッジコンピューティングや組み込みシステムなど、リソース制約のある環境での需要拡大が期待できる。",
    "既存のpg_notifyやHueyと比較して、SQLiteエコシステム内で完結するシンプルさが差別化要因となり、周辺ツール群の成長次第ではデータ基盤の選択肢を広げる可能性がある。",
  ].join("\n\n"),
} as const;

export type MockArticle = typeof MOCK_ARTICLE;
