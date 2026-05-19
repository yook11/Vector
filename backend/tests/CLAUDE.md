# backend/tests/ — テストガイド

バックエンドの全テストをここに配置する。pytest + pytest-asyncio + httpx (AsyncClient) を使用。

## テストルール

### 全般
- テスト関数名は `test_` プレフィックス + 何をテストしているか明示
  - 例: `test_fetch_news_skips_duplicate_urls`
- 非同期テストには `@pytest.mark.asyncio` を付与
- 1テスト = 1アサーション（原則）

### フィクスチャ (conftest.py)
- `setup_db` (autouse): integration テストのみ各テスト前に `create_all` / 終了後 `drop_all`。`auth."user"` を seed (unit テストは DDL を流さない)
- `session_factory`: Service クラステスト用の `async_sessionmaker`
- `db_session`: テスト用 AsyncSession (`expire_on_commit=False`)
- `client`: DI でセッション差し替え済みの未認証 httpx.AsyncClient
- `auth_headers`: 通常ユーザー用 BFF プロキシ認証ヘッダー
- `authed_client`: 通常ユーザー認証済み httpx.AsyncClient
- `admin_client`: 管理者 (role=admin) 認証済み httpx.AsyncClient
- `sample_categories`: Category 3件 (ai / computing / semiconductor)
- `sample_source`: RSS ニュースソース
- `sample_hn_source`: Hacker News API ソース
- `sample_av_source`: Alpha Vantage API ソース
- テストDBは db-test 上の `vector_test` を使用 (conftest が migration role で create/drop、`DATABASE_URL` の DB 名は無視され常に `vector_test`)

### モック方針
- 外部API（Gemini, RSS取得）は必ずモック
- `unittest.mock.AsyncMock` または `pytest-mock` を使用
- DB操作はモックせず、テストDBに対して実行

### カバレッジ
- サービス層: 主要パス + エラーケース
- ルーター: 正常系 + 404/409 等のエラーレスポンス
- モデル: バリデーションの境界値

## テスト設計原則

- **仕様の不変条件を書く**
  期待値は実装を走らせた出力からではなく、仕様から決める。具体値が結果的に一致してもよいが、根拠は仕様であること。時間・順序・乱数に依存する値は形だけを見て、値を固定しない。緑であること自体を仕様の根拠にしない。

- **その抽象レベルの契約を検証する**
  契約はそれを名乗る公開 API で確かめ、下の層の内部配線を覗いて間接確認しない。差し替えは clock / network / 外部 API / ファイル I/O など、境界外の不安定依存に限る。複雑な純粋ロジックを単体で直接テストするのは正当。その関数の契約はその signature にある。

- **テストを非空虚にする**
  検証したい境界・欠損・重複・異常形が入力に実在し、assert がその差で実際に落ちること。ケースが在っても assert が弱く区別しなければ空虚。その理由で落ちないテストは、不変条件を破る実装を見逃す。

- **導ける期待値は入力から導く**
  件数・存在・通過など、入力構造から導ける期待値は fixture / test data から算出する。入力由来でない仕様定数は直書きしてよいが、由来をコメントで残す。標本更新時に「仕様が壊れた」のか「標本が変わった」のか分かる状態を保つ。

- **実装規則をテストに複製しない**
  閾値・正規表現・分岐を再実装せず、期待値をテスト内で production 関数を呼んで作らない。同一ロジックの二重実装は tautology。見るのは入力と公開出力の間の振る舞い境界。

- **不変条件ごとに所有テストを決める**
  各不変条件の正本テストを 1 つ定める。別レベルから副次的に触れる重複は可だが、正本がどこかを曖昧にしない。仕様変更時の更新箇所が一意になる状態を保つ。

## 参照ドキュメント

- `backend/CLAUDE.md` — バックエンド全体のルール
- `backend/app/models/` + `backend/alembic/versions/` — テーブル定義 (SQLModel + Alembic migration が SSoT)
- `backend/app/schemas/` — 期待するレスポンス形式 (Pydantic v2 が API SSoT、FastAPI が `/openapi.json` を自動生成)
