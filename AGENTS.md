# Vector — Agent Working Contract

海外テックニュース収集・AI翻訳・投資分析ダッシュボード。
コミット・PR は日本語で作成する。

## Scope

- 依頼の完了条件を満たしたら止め、直接関係しない改善は提案に留める。
- 将来の拡張性や一般論だけを理由に、抽象化・設定・fallback・依存を追加しない。

## Source Of Truth

- API の SSoT は FastAPI の Pydantic schemas。
- DB 変更は Alembic migration 経由のみ。
- 環境変数は設定層経由で扱い、`.env` を読まない・表示しない・編集しない。
- 認証・認可ロジックを簡略化、迂回、無効化しない。

## Public Repository Hygiene

- ポートフォリオの公開 frontend URL は公開導線として commit してよい。
- それ以外の実 production の Fly app 名、internal hostname、deploy / rollback / restore の具体手順は commit しない。
- `fly*.toml` は portfolio 用の構成例として公開し、app 名や URL は placeholder にする。
- 本番 deploy に必要な実値は GitHub Environment secrets / Fly secrets / private runbook 側で管理する。
- docs では設計意図と境界を説明し、運用者だけが使う詳細手順や復旧コマンドは公開しない。

## Comments

- コメント・docstring は、コードから読めない理由・制約・外部仕様を補う場合だけ、日本語で原則1文で書く。
- 変更していないコードにコメントを追加しない。
- テストには、何を確かめるテストかを日本語1文で書く。ファイル冒頭の docstring に対象と前提(実DB・モックなど)を書き、各テストの docstring(frontend は `it` の名前)に確かめる振る舞いを書く。

## Verification

- 実装変更後は `/check` スキルで検証する。検証できなかった項目は理由とともに明記する。
- テストを通すために機能を削除・無効化しない。
- テストの重複排除は同じ振る舞いの検証場所を揃えることであり、異なる条件・期待結果のケースを一つの関数へ統合することではない。各ケースの条件・操作・期待結果が読める構成にし、複数対象を同時に扱うのはその相互作用を検証する場合とする。

## Delegation

- サブエージェントにastraを使用しない。
- サブエージェントは、ユーザーが調査を依頼したときか、影響範囲を事前に列挙できないときだけ起動する。報告は実際のコードと照合してから採用する。
- 実装系サブエージェント(test-writer など)は起動しない。レビュー系は、ユーザーがレビューを依頼したときだけ起動する。

## Ask First

次は事前確認する。

- DB schema / SQLModel model の変更
- 新規 dependency の追加
- API response shape の破壊的変更
- 認証・認可ロジックの変更
- 複数レイヤーにまたがる再設計
- 大きな構成変更、または既存境界の移動
