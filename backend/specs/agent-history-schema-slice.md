# Agent 会話履歴 DB schema 実装 slice 仕様 (Slice 1)

## 位置付け

親仕様: `specs/agent/conversation-history-async-runs.md`（合意済み。specs/ は gitignored の
ローカル正本のため、別 worktree・fresh checkout からは見えない）。
本 slice は 4 テーブル（`agent_threads` / `agent_messages` / `agent_message_sources` /
`agent_runs`）の Alembic migration と SQLAlchemy model を作る。**データ層のみ**で、
API・worker・保存経路・Redis・frontend は後続 slice（2〜5）。

後続 slice が消費する契約は「テーブル + 制約」そのもの。ここで構造的保証
（unique / check / partial unique index / FK cascade）を DB に焼き切ることが本 slice の価値。

実装中の `y1_agent_history.py` / model は未コミット差分のため、source 表示契約変更
（`snippet` 削除、external `evidence_claim` 化）は follow-up migration ではなく本 slice に
直接畳み込む。

## Problem

会話履歴と実行状態を永続化するテーブルが存在しない。親仕様の Invariants
（user 分離 / 1 thread 1 active run / 1 user message 1 run / 表示契約の完全再現 /
物理削除 cascade）を DB 制約として強制した schema を作る。

## Evidence (調査済みの既存規約)

- **migration 規約**: `alembic/versions/x7_query_embedding_cache.py` が新規テーブル追加の直近前例。
  short prefix の revision ID（`alembic_version.version_num` は varchar(32) のため **32 字以内必須**）、
  docstring に目的、`MIGRATION_KIND = "expand"`（新規テーブルのみ → contract gate 不要、CI 自動適用）。
  現 head は `x8_published_at_not_null`（実装時に `alembic heads` で再確認）。
- **model 規約**: `app/models/query_embedding_cache.py` — `mapped_column` + `Mapped[T]`、
  制約は `__table_args__` に名前付きで定義、`created_at` は
  `DateTime(timezone=True), server_default=func.now()`。
- **model / migration の二重定義は必須**: `tests/conftest.py:184-186` はテスト schema を
  `Base.metadata.create_all` で作る（migration を流さない）。制約が model に無いと
  テストで検証できず、migration に無いと本番に届かない。**両方に同一の制約を書く**。
  parity の検証前例は `tests/test_analyzed_articles_db_contract.py`（FK target を introspection で assert）。
- **auth.user FK**: `app/models/watchlist_entry.py:22` が `ForeignKey("auth.user.id", ondelete="CASCADE")` の前例。
  metadata 解決は `app/models/auth_ref.py`。テストは `tests/conftest.py:180` で auth schema を作り
  `conftest.py:208` で auth.user を seed 済み（本 slice のテストもこの fixture に乗る）。
- **JSONB list**: `app/models/weekly_briefing.py:65-67` が `Mapped[list[...]] = mapped_column(JSONB)` の前例。
- **uuid pk の前例は app テーブルに無い**（auth.user 参照のみ）→ 生成戦略は本 slice の新規決定（設計判断 1）。
- **model 追加時は `app/models/__init__.py` への登録が必要**（metadata 登録の流儀）。
- **migration gate の index 規則**: `scripts/migration_gate.py:226-230` は `op.create_index` を
  `postgresql_concurrently=True` 無しでは manual-only に分類する（既存テーブルの lock 保護が目的）。
  一方 `op.create_table` 内に inline で渡す `sa.Index` は分類対象外で expand として通り、
  前例あり（`alembic/versions/d1e2f3a4b5c6_create_discovered_articles_and_articles.py:77`）。
  新規空テーブルの index 作成は瞬時で lock 問題が構造的に存在しないため、これは gate の
  迂回ではなく設計意図どおりの通し方（設計判断 10）。

## 設計判断

1. **uuid pk は DB 側生成 `server_default=gen_random_uuid()`**（PostgreSQL 13+ 組込み、
   extension 不要、Neon / テスト用実 Postgres とも利用可）。id 生成を永続化境界に置き、
   SQL 直 insert でも一意性が壊れない。asyncpg dialect の implicit RETURNING で
   flush 後に Python 側から取得できるため、202 応答（threadId / runId）にも支障がない。
2. **enum 系列（role / status / progress_stage / kind）は `String(32)` + 名前付き CHECK**。
   PG native enum は使わない（値追加が migration の型変更になり重い）。Python 側の
   StrEnum 化は消費側 slice（2 以降）で必要になった時に行い、本 slice は DB 制約のみ。
3. **updated_at は「最終活動時刻」で app 管理**。`onupdate` 自動更新はしない
   （title rename 等の無関係な UPDATE で一覧ソート順が動くのを防ぐ。bump は
   保存経路＝slice 2 の責務で、message 追加 tx 内で明示的に行う）。
   初期値のみ `server_default=func.now()`。
4. **ORM relationship は定義しない**。削除伝播は DB の `ondelete` に任せ（ORM cascade 不使用）、
   read 経路の relationship は consumer が現れる slice 2 / 3 で追加する。
5. **run と回答の対応を CHECK で焼く**: `(status = 'completed') = (assistant_message_id IS NOT NULL)`。
   「完了 run は必ず回答を持ち、未完了・失敗 run は回答を持たない」を構造的に保証する。
   started_at / completed_at の時刻整合 CHECK は**入れない**（sweeper が queued のまま
   failed に倒すと started_at NULL の failed が正当に存在するため、時刻列に不変条件は張れない）。
6. **missing_aspects の役割制約と JSON array 型を CHECK で焼く**:
   `role = 'assistant' OR missing_aspects = '[]'::jsonb`（user message は常に空）と
   `jsonb_typeof(missing_aspects) = 'array'`。要素が非空 string であることは slice 2 の
   書き込みファクトリが Pydantic contract で保証する。
7. **seq / ordinal は 1 始まりの正数**（CHECK >= 1）。ordinal は表示順、source_ref は
   citation `[[n]]` の結合キーで役割が異なるため両方保存（親仕様どおり）。
8. **source kind ごとの恒常条件を CHECK で焼く**。
   external_url は `url` と `evidence_claim` が必須で、`analyzed_article_id` は NULL。
   URL は app-layer の `SafeUrl` を正としつつ、DB では `http(s)` scheme と 2048 字上限の
   backstop を置く。internal_article は `url` / `evidence_claim` / `source_name` を NULL にする。
   ただし internal の `analyzed_article_id` は `SET NULL` で欠けうるため NOT NULL 制約や
   CHECK は張らない。保存時の非 NULL は slice 2 の mapper が保証し、記事削除後の NULL は
   正当な永続状態として扱う。
9. **revision ID は `y1_agent_history`**（16 字、32 字制限内。y 系列を本機能群で使う）。
10. **index はすべて `op.create_table` 内の inline `sa.Index` で作る**（partial unique index 含む）。
    単独の `op.create_index` は gate が concurrently を要求するが、新規空テーブルに
    CONCURRENTLY を使うと autocommit block が必要になり、失敗時に invalid index が残る
    failure mode まで背負う。空テーブルでは得るものが無いので inline 方式（前例あり、
    Evidence 参照）を採る。
11. **run と message の同一 thread 整合を composite FK で焼く**。thread_id / user_message_id /
    assistant_message_id を独立 FK にすると「run の thread と message の thread が別」という
    不整合行を DB が許す。agent_messages に `unique(thread_id, id)`（pk の superkey）を置き、
    runs 側は `(thread_id, user_message_id)` / `(thread_id, assistant_message_id)` の
    composite FK で参照する。assistant_message_id が NULL の間は MATCH SIMPLE により
    FK 検査対象外（未完了 run の正当な状態）で、値が入る時点で同一 thread が強制される。
12. **failed ⇔ error_code を双方向 CHECK で焼く**: `(status = 'failed') = (error_code IS NOT NULL)`。
    API は failed 時に errorCode を返す契約なので片方向は必須。逆方向も、error_code の
    consumer は failed 表示のみで、非 failed 行に残ると polling 応答の導出が曖昧になるため締める。
13. **表示再現に必要な文字列の非空を CHECK で焼く**。contract は source_ref / title を
    min_length=1 で保証しており、DB 側が空文字を許すと「保存できるが `ResearchResponse` に
    戻せない行」が作れてしまう（Invariant「表示契約の完全再現」の穴）。threads.title も同様。
14. **source は assistant message にのみ付く、は app-layer invariant とする**。DB で焼くには
    sources へ role 列を複製した composite FK が必要で、この列に他の用途（consumer）が無い。
    書き込み経路は slice 2 の単一ファクトリに閉じるため、そこで構造化し slice 2 のテストで
    固定する（user message + sources の insert を拒否することの検証）。
15. **履歴 read の internal article id は nullable**。live 応答は回答直後のため
    `articleId` required のままでよいが、履歴行は analyzed article 削除後に
    `analyzed_article_id = NULL` が正当になる。slice 3 の履歴 read schema は
    `articleId: int | null` として、この違いを API contract に出す。
16. **commit 前に専用 feature branch へ分離する**。2026-07-09 時点の未コミット差分は
    `refactor/agent-evidence-collection-rename` 上にあるため、履歴 schema 作業として commit する前に
    専用 branch へ移す。

## DDL（migration / model 両方に同一定義）

```
agent_threads
- id          uuid pk  server_default gen_random_uuid()
- user_id     uuid not null  FK auth.user(id) ondelete CASCADE
- title       text not null
- created_at  timestamptz not null  server_default now()
- updated_at  timestamptz not null  server_default now()

  ix_agent_threads_user_updated      (user_id, updated_at desc, id desc)
  ck_agent_threads_title_not_empty   title <> ''

agent_messages
- id              uuid pk  server_default gen_random_uuid()
- thread_id       uuid not null  FK agent_threads(id) ondelete CASCADE
- seq             integer not null
- role            varchar(32) not null
- content         text not null
- missing_aspects jsonb not null  server_default '[]'::jsonb
- created_at      timestamptz not null  server_default now()

  uq_agent_messages_thread_seq            unique (thread_id, seq)
  uq_agent_messages_thread_message        unique (thread_id, id)   -- runs の composite FK の参照先 (pk の superkey)
  ck_agent_messages_role                  role in ('user', 'assistant')
  ck_agent_messages_seq_positive          seq >= 1
  ck_agent_messages_content_not_empty     content <> ''
  ck_agent_messages_missing_aspects_role  role = 'assistant' or missing_aspects = '[]'::jsonb
  ck_agent_messages_missing_aspects_array jsonb_typeof(missing_aspects) = 'array'

agent_message_sources
- id                  bigserial pk
- message_id          uuid not null  FK agent_messages(id) ondelete CASCADE
- ordinal             integer not null
- kind                varchar(32) not null
- source_ref          text not null
- analyzed_article_id integer null  FK analyzed_articles(id) ondelete SET NULL
- url                 text null
- title               text not null
- source_name         text null
- published_at        timestamptz null
- evidence_claim      text null

  uq_agent_message_sources_message_source_ref  unique (message_id, source_ref)
  uq_agent_message_sources_message_ordinal     unique (message_id, ordinal)
  ck_agent_message_sources_kind                kind in ('internal_article', 'external_url')
  ck_agent_message_sources_ordinal_positive    ordinal >= 1
  ck_agent_message_sources_source_ref_not_empty  source_ref <> ''
  ck_agent_message_sources_title_not_empty     title <> ''
  ck_agent_message_sources_external_url        kind <> 'external_url'
                                                or (
                                                  url is not null
                                                  and url ~* '^https?://'
                                                  and char_length(url) <= 2048
                                                  and analyzed_article_id is null
                                                  and evidence_claim is not null
                                                  and evidence_claim <> ''
                                                )
  ck_agent_message_sources_internal_article    kind <> 'internal_article'
                                                or (
                                                  url is null
                                                  and source_name is null
                                                  and evidence_claim is null
                                                )

  ※ 「source は assistant message にのみ付く」は app-layer invariant（設計判断 14、slice 2 で固定）
  ※ internal の analyzed_article_id は insert 時 app-layer で非 NULL 保証。
     記事削除後は ON DELETE SET NULL により NULL が正当状態（設計判断 8 / 15）。

agent_runs
- id                   uuid pk  server_default gen_random_uuid()
- thread_id            uuid not null  FK agent_threads(id) ondelete CASCADE
- user_message_id      uuid not null
- assistant_message_id uuid null
- status               varchar(32) not null
- progress_stage       varchar(32) null
- error_code           text null
- created_at           timestamptz not null  server_default now()
- started_at           timestamptz null
- completed_at         timestamptz null

  FK (thread_id, user_message_id)      → agent_messages(thread_id, id) ondelete CASCADE
  FK (thread_id, assistant_message_id) → agent_messages(thread_id, id) ondelete CASCADE
                                         -- composite FK で run と message の同一 thread を強制 (設計判断 11)
                                         -- assistant_message_id NULL の間は MATCH SIMPLE で検査対象外
  uq_agent_runs_user_message      unique (user_message_id)
  uq_agent_runs_assistant_message unique (assistant_message_id)  -- NULL 複数可
  uq_agent_runs_thread_active     unique (thread_id) where status in ('queued', 'running')
  ix_agent_runs_thread            (thread_id)
  ck_agent_runs_status            status in ('queued', 'running', 'completed', 'failed')
  ck_agent_runs_progress_stage    progress_stage in ('planning', 'retrieving', 'synthesizing')
  ck_agent_runs_completed_answer  (status = 'completed') = (assistant_message_id is not null)
  ck_agent_runs_failed_error      (status = 'failed') = (error_code is not null)
```

作成順: threads → messages → sources → runs（FK 依存順。messages の
`uq_agent_messages_thread_message` が runs の composite FK 参照先のため先行必須）。
downgrade は逆順 drop。index は partial unique index 含めすべて `op.create_table` 内の
inline `sa.Index`（設計判断 10）、model 側は `__table_args__` の
`Index(..., unique=True, postgresql_where=text(...))` / `ForeignKeyConstraint` で
同一定義（create_all がテスト正本のため省略不可）。

## New Types / Structure

```
backend/alembic/versions/y1_agent_history.py   (新規: 4 テーブル, MIGRATION_KIND="expand")
backend/app/models/agent_thread.py             (新規: AgentThread)
backend/app/models/agent_message.py            (新規: AgentMessage, AgentMessageSource)
backend/app/models/agent_run.py                (新規: AgentRun)
backend/app/models/__init__.py                 (登録追加)
backend/tests/test_agent_history_db_contract.py (新規: 制約・cascade 検証)
```

message と source は「表示用会話履歴」として不可分（source は message の子で単独の
意味を持たない）ため同一 module。run は責務（実行状態）が違うため別 module。

## Invariants

- DB schema 変更は Alembic 経由のみ。migration は expand（破壊系・`op.execute` なし）。
- model と migration の制約定義は同一（テストは create_all、 本番は migration が正本のため）。
- 認証・認可には触れない（データ層のみ）。
- 既存テーブル・既存 migration に変更を加えない。
- `.env` を読まない・表示しない・編集しない。

## Non-goals

- API / router / schemas の変更（slice 2〜3）。
- worker task・保存経路・seq 採番ロジック・thread 所有権チェック（slice 2。
  `SELECT FOR UPDATE` は保存経路の実装であり schema には現れない）。
- live / history read の Pydantic schema 実装。ただし slice 3 では履歴 internal source の
  `articleId` を nullable にする（設計判断 15）。
- ORM relationship / StrEnum ドメイン型（consumer が現れる slice で追加）。
- 「source は assistant message にのみ付く」の app-layer 強制（設計判断 14。slice 2 の
  書き込みファクトリ + テストへの申し送り）。
- Redis イベント（slice 5）。
- 本番への migration 適用（expand のため CI 自動適用に乗る。手動 `migrate-prod` 不要）。

## Tests（構造的保証の検証 — Red-first）

`tests/test_agent_history_db_contract.py`。conftest の auth.user seed fixture に乗る。

1. cascade: user 削除 → threads / messages / sources / runs が連鎖削除される。
2. cascade: thread 削除 → messages / sources / runs が連鎖削除される。
3. SET NULL: analyzed_article 削除 → source 行は残り analyzed_article_id が NULL、
   title 等 snapshot は保持される。
4. `uq_agent_runs_thread_active`: 同一 thread に queued/running の run を 2 本 insert
   → IntegrityError。completed/failed が既にある thread への新規 queued は通る。
5. `uq_agent_runs_user_message`: 同一 user_message_id で 2 run → IntegrityError。
6. `ck_agent_runs_completed_answer`: completed + assistant NULL / running + assistant 非 NULL
   がともに拒否される。
7. `uq_agent_runs_assistant_message`: 同一 assistant_message_id を 2 run に使う → IntegrityError。
8. `ck_agent_runs_failed_error`: failed + error_code NULL / completed + error_code 非 NULL
   がともに拒否される。
9. composite FK（設計判断 11）: 別 thread の message を user_message_id / assistant_message_id に
   指す run の insert → IntegrityError。同一 thread の message なら通る（正パス）。
10. `uq_agent_messages_thread_seq`: 同一 (thread_id, seq) 重複拒否。
11. `ck_agent_messages_missing_aspects_role`: user message に非空 missing_aspects → 拒否。
12. `ck_agent_messages_missing_aspects_array`: object / string / null の missing_aspects → 拒否。
13. `ck_agent_message_sources_external_url`: kind=external_url + url NULL / 非 http(s) / 2048 字超 /
    evidence_claim NULL / evidence_claim 空文字 / analyzed_article_id 非 NULL → 拒否。
14. `ck_agent_message_sources_internal_article`: kind=internal_article + url 非 NULL /
    source_name 非 NULL / evidence_claim 非 NULL → 拒否。analyzed_article_id NULL は削除後状態として通る。
15. 非空 CHECK 境界: threads.title / messages.content / sources.source_ref / sources.title の
    空文字 insert が拒否される。
16. check 系境界: role / status / progress_stage / kind の範囲外値が拒否される。
17. uuid pk: id 未指定 insert で DB 側生成された uuid が返る（server_default の実効確認）。
18. parity: FK target / ondelete（composite FK 含む）を introspection で assert
    （`test_analyzed_articles_db_contract.py` の流儀）。

テスト設計・追加は test-writer agent に分担する。

## 検証

- `/migration` の手順でローカル往復: `alembic upgrade head` → `alembic downgrade -1` →
  再 `upgrade head`（dev DB。ORM 変更後の container restart に注意）。
- `/check`（lint / format / types / tests）。

## Done

- 4 テーブルが migration・model の両方に同一制約で定義され、
  `alembic upgrade head` / `downgrade -1` の往復がローカル dev DB で成功する。
- 上記テストが green（DB 制約が実際に効いていることの実証込み）。
- 既存 suite に regression なし。
- 本 slice で停止（API / worker への結線は slice 2 の仕様書合意後）。
