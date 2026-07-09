# Vector — Agent Working Contract

海外テックニュース収集・AI翻訳・投資分析ダッシュボード。

## Work Definition

実装前に定義すること。

1. Problem: 今回解く問題を簡潔に定義する。
2. Evidence: 関連する仕様・schema・test・設定・既存実装を確認する。
3. Invariants: 持続的に守るべき振る舞い・制約・境界条件を定義する。
4. Non-goals: 今回やらないことを明確にする。
5. Done: 達成すべき状態と、作業を停止できる条件を定義する。

## Scope Rules

- 既存実装は正解ではなく証拠候補として扱う。
- 既存パターンを踏襲する前に、同じ責務・同じ境界・同じ失敗条件を扱っているか確認する。
- 変更は Problem / Invariants / Done に対して必要十分な範囲に収める。
- Done を満たしたら停止し、周辺改善は提案に留める。
- 新しい抽象化・設定・fallback は、現在の Problem / Invariants / Done をより単純に満たす場合のみ追加する。
- 重複排除だけを目的に抽象化しない。抽象化する場合は、同じ契約が複数箇所にあり、責務と境界が一致していることを確認する。
- 将来の拡張性だけを理由に抽象化しない。

## Source Of Truth

- API の SSoT は FastAPI の Pydantic schemas。
- DB 変更は Alembic migration 経由のみ。
- 環境変数は設定層経由で扱い、`.env` を読まない・表示しない・編集しない。
- 認証・認可ロジックを簡略化、迂回、無効化しない。

## Public Repository Hygiene

- 実 production の Fly app 名、internal hostname、deploy / rollback / restore の具体手順は commit しない。
- `fly*.toml` は portfolio 用の構成例として公開し、app 名や URL は placeholder にする。
- 本番 deploy に必要な実値は GitHub Environment secrets / Fly secrets / private runbook 側で管理する。
- docs では設計意図と境界を説明し、運用者だけが使う詳細手順や復旧コマンドは公開しない。

## Research

- ライブラリやフレームワークの使用方法は、憶測で実装せず、`/research` スキルや最新の公式ドキュメントで確認する。
- 設計や実装方針に迷った場合は、既存実装だけで判断せず、現在の Problem / Invariants / Done に近い一次情報・ベストプラクティスを調べる。
- 外部情報は、現在の問題に必要な判断材料として使い、一般論や流行を理由に不要な抽象化・依存・構成変更を追加しない。

## Memory

- メモリは作業ログではなく、今後も使う永続的な情報だけに使う。
- 迷ったら記録せず、必要なら事前に確認する。

## Comments

- コメント・ドックストリングは、コードだけでは読み取りにくい理由・制約・不変条件・外部仕様を補う場合のみ追加する。
- コメントは原則1文で簡潔に書く。
- 処理内容をなぞるだけのコメントは書かない。
- コメントで実装を正当化しない。説明が長くなる場合は、設計の歪みを疑う。
- 変更していないコードにコメントやドックストリングを追加しない。

## Verification

- 実装変更後は `/check` スキルで検証する。
- テストを通すために機能を削除・無効化しない。
- 検証できなかった場合は、未実行の項目と理由を明記する。

## Failure Learning

- Problem / Done と実際の作業にズレが見えた場合は、失敗から学ぶために `/failure-log` を使う。

## Task Agents

- frontend UI / component / page 実装は、利用可能な場合 frontend-ui-builder agent に分担する。
- テスト設計・単体テスト追加は、利用可能な場合 test-writer agent に分担する。

## Ask First

次は事前確認する。

- DB schema / SQLModel model の変更
- 新規 dependency の追加
- API response shape の破壊的変更
- 認証・認可ロジックの変更
- 複数レイヤーにまたがる再設計
- 大きな構成変更、または既存境界の移動
