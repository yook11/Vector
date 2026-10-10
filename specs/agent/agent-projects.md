# Agent Projects Spec

Status: Draft
Updated: 2026-10-04
Scope: スレッドをまとめるプロジェクトのテーブル設計と、プロジェクト・スレッド一覧の取り方

## Problem

利用者がスレッドをプロジェクトにまとめ、最近使った順にたどれるようにする。現状のスレッドは利用者に直接
ぶら下がるだけで、まとめる単位がない。

## Evidence

- `backend/app/models/agent_thread.py`: スレッドの所有者は`user_id`だけが持つ。一覧のindexは
  `(user_id, updated_at DESC, id DESC)`。
- `backend/app/models/agent_run.py`: runとmessageが同じスレッドに属することを複合FK`(thread_id, message_id)`で
  保証している。スレッドとプロジェクトの所有者の一致も同じ方式で保証できる。
- `backend/app/agent/running/creation.py`: スレッドは`POST /api/v1/research/responses`の初回質問で暗黙に作られ、
  質問の作成時に`updated_at`を更新する。
- `backend/app/agent/running/completion.py`: 回答の完了時にworkerが`updated_at`を更新する。
- `backend/app/agent/threads/repository.py`: 既存のスレッド一覧はOFFSET方式。
- `specs/pipeline/database-role-permissions.md`: Better Authは`vector_auth`で`auth`スキーマだけを書く。
  利用者の登録時に`public`の表へ行を作ると、この境界を越える。
- `specs/platform/api-role.md`: 外部キーの動作は参照側の表の所有者権限で動く。FOR UPDATEは対象表の
  1列以上のUPDATE権限を要する。
- `frontend/src/features/research/components/DeleteThreadButton.tsx`: 取り消せない削除はAlertDialogで確認している。
- 本番とlocalのPostgreSQLは17。`ON DELETE SET NULL (列)`は15以降で使える。
- ローカルのPostgreSQL 17で、一時テーブルに架空データを入れて実行計画を確認した(2026-10-04)。
  - プロジェクト一覧: `LATERAL ... LIMIT 1`は下記indexのIndex Only Scanで1プロジェクト1行だけを読み、
    20件の並べ替えはメモリ25kBで済んだ。`GROUP BY`と`max()`ではプロジェクト内の全スレッドを読んだ。
  - スレッド一覧: カーソル条件`(updated_at, id) < (…)`はindexの検索条件になり、100万行でも並べ替えなしで
    続きから読んだ。

## Decisions

1. プロジェクトは`agent_projects`で持つ。利用者が所有し、名前は利用者内で一意とする。
2. スレッドは多くても1つのプロジェクトに属する。`agent_threads.project_id`で表し、中間テーブルは持たない。
3. `project_id IS NULL`は「プロジェクトに入っていない」状態を表す。デフォルトのプロジェクトは作らない。
4. スレッドはプロジェクト間、およびプロジェクト外との間で移動できる。移動では`updated_at`を更新しない。
5. プロジェクトを削除しても配下のスレッドは削除せず、プロジェクト外へ戻す。削除前の確認ダイアログで、
   配下のスレッド件数、スレッドは消えないこと、操作を取り消せないことを示す。
6. プロジェクト一覧は最近使った順に並べる。最近使った時刻は`GREATEST(created_at, 配下スレッドの最新updated_at)`を
   毎回集計し、列には持たない。スレッドを移しても、移した先のプロジェクトは上に来ない。
7. プロジェクト内のスレッドは、プロジェクトを開いたときに取得する。20件ずつカーソル方式で追加取得する。
8. カーソルは、最後に返した行の`(updated_at, id)`をJSONにしてbase64urlで文字列にしたものとし、backendが作る。
   frontendは中身を解釈せず、そのまま送り返す。

## Schema

```sql
CREATE TABLE agent_projects (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES auth."user"(id) ON DELETE CASCADE,
  name       text NOT NULL CHECK (name <> ''),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, name),
  UNIQUE (user_id, id)
);

ALTER TABLE agent_threads
  ADD COLUMN project_id uuid NULL,
  ADD FOREIGN KEY (user_id, project_id)
      REFERENCES agent_projects (user_id, id)
      ON DELETE SET NULL (project_id);

CREATE INDEX ix_agent_threads_user_project_updated
  ON agent_threads (user_id, project_id, updated_at DESC, id DESC);
```

- `UNIQUE (user_id, id)`は複合FKの参照先にする。
- 追加するindexは、プロジェクト内の一覧、プロジェクト外の一覧(`project_id IS NULL`)、プロジェクト一覧の
  最新スレッドの検索、削除時に`SET NULL`する行の検索を担う。
- エージェント機能の全テーブルの関係は`docs/agent/data-model.md`のER図にまとめる。

## Queries

プロジェクト一覧:

```sql
SELECT p.id, p.name, GREATEST(p.created_at, lt.updated_at) AS last_active_at
FROM agent_projects p
LEFT JOIN LATERAL (
  SELECT t.updated_at FROM agent_threads t
  WHERE t.user_id = p.user_id AND t.project_id = p.id
  ORDER BY t.updated_at DESC LIMIT 1
) lt ON true
WHERE p.user_id = :user_id
ORDER BY last_active_at DESC, p.id DESC;
```

プロジェクト内のスレッド一覧(カーソル条件は2ページ目以降だけ付ける):

```sql
SELECT id, title, updated_at FROM agent_threads
WHERE user_id = :user_id AND project_id = :project_id
  AND (updated_at, id) < (:cursor_updated_at, :cursor_id)
ORDER BY updated_at DESC, id DESC
LIMIT 21;
```

21件目があれば続きがあるとし、20件目から次のカーソルを作る。件数のCOUNTは取らない。

## Invariants

- スレッドとプロジェクトの所有者は一致する。複合FK`(user_id, project_id)` → `agent_projects (user_id, id)`で
  DBが保証する。
- プロジェクトの削除で変わるのはスレッドの`project_id`だけで、`user_id`・メッセージ・runは変わらない。
- 「最近使った」の正本は`agent_threads.updated_at`だけとする。
- カーソルで変えられるのは、利用者自身のプロジェクト内での読み始めの位置だけとする。`user_id`はセッション、
  `project_id`はパスから取る。不正なカーソルは400とする。
- `vector_agent`は`agent_projects`を読まず、`project_id`を更新しない。

## Permissions

- `vector_api`: `agent_projects`にSELECT・INSERT・DELETEと`name`列のUPDATEを付与する。`agent_threads`には
  `project_id`列のUPDATEを追加する。
- `vector_agent`: 変更しない。
- 削除時の`SET NULL`は外部キーの動作として表の所有者権限で動くため、削除のための追加権限は要らない。

## Non-goals

- 1つのスレッドを複数のプロジェクトに入れること
- プロジェクト単位で指示や調査の申し送り(`research_handoff`)を共有すること
- デフォルトのプロジェクト
- 既存の全スレッド一覧(OFFSET方式)の変更

## Open Questions

- 名前の比較規則と長さの上限。案は、前後の空白を除いて保存し、大文字・小文字は区別する。
- プロジェクト内で新しいスレッドを始める手段。`POST /responses`に`project_id`を渡すか。
- 全スレッド一覧を残すか、プロジェクト外のスレッド一覧に置き換えるか。置き換えるなら既存の
  `ix_agent_threads_user_updated`を削除できる。
- APIの形(エンドポイント、一覧の返却項目、カーソルの名前)。

## Done

- [ ] Alembic migrationで`agent_projects`、`agent_threads.project_id`、複合FK、indexを追加する。
- [ ] `vector_api`の権限をmigrationで付与し、`backend/local_tests/permissions/test_api_permissions.py`、
      `specs/pipeline/database-role-permissions.md`、`specs/platform/api-role.md`を更新する。
- [ ] プロジェクトの作成・改名・削除・一覧、プロジェクト内のスレッド一覧、スレッドの移動をAPIで提供する。
- [ ] 実DBの試験で次を確認する。
  - 他の利用者のプロジェクトへスレッドを移せない。
  - プロジェクトを削除してもスレッドが残り、`project_id`だけがNULLになる。
  - プロジェクト一覧が最近使った順になる。
  - 並びが変わっても、カーソルで取った続きに重複・抜けが出ない。
- [ ] frontendにプロジェクト削除の確認ダイアログを置く。
- [ ] `docs/agent/data-model.md`の「予定」の表記を外す。

## Implementation

未着手。

## Verification

未実施。
