# ADR-004: Service レイヤーの Unit of Work 規約

> 日付: 2026-04-12 / ステータス: Accepted

## Context

Vector の HTTP 経路は dependency-managed transaction で動く。
`app/dependencies.py` の `get_session` が `async with session.begin():`
ブロックで session を yield し、リクエストの正常終了で commit、例外で
rollback する設計。詳細な議論経緯は
`specs/backlog/transaction-boundary-and-rich-model.md` を参照。

この基盤の上で Rich Domain Model 化を進めるにあたり、Service レイヤーが
**既存エンティティの状態変更後に明示的な永続化呼び出し（`repo.save(entity)`
など）を行うかどうか** を決める必要があった。

## Alternatives

| 案 | 内容 | 評価 |
|---|---|---|
| 立場 A: UoW 依存 | mutate のみ。`session.begin()` ブロックの commit 時に SQLAlchemy の Unit of Work が自動で flush + UPDATE。Service は既存エンティティに対して `save` を呼ばない | **採用** |
| 立場 B: 明示 save | `save` を新規・既存両方扱う UPSERT として運用。Service は状態変更後に常に `await repo.save(entity)` を明示的に呼ぶ。Django/Rails の `model.save()` と同じセマンティクス | 不採用 |

## Decision

**Vector の Service は、既存エンティティの状態変更に対して明示的な
永続化呼び出しを行わない。** SQLAlchemy の Unit of Work パターンに従い、
`get_session` dependency で取得したオブジェクトに対するメモリ上の
属性変更は、dependency の `async with session.begin():` ブロックが
閉じる瞬間に自動的に flush + commit される。この仕組みを前提に
Service コードを読むこと。**新規エンティティの作成と削除は明示的に
Repository 経由で行う。**

### 規約の具体例

```python
# 既存エンティティの状態変更: Model のドメインメソッドで mutate するだけ
async def activate_source(self, source_id: int) -> NewsSourceDetail:
    source = await self._get_or_raise(source_id)
    source.activate()  # ← repo.save() は呼ばない
    return NewsSourceDetail.model_validate(source)

# 新規エンティティの作成: Repository.create を明示的に呼ぶ
async def create_source(self, body: NewsSourceCreate) -> NewsSourceDetail:
    source = NewsSource(...)
    await self.repo.create(source)  # ← 明示
    return NewsSourceDetail.model_validate(source)

# 削除: Repository.delete を明示的に呼ぶ
async def delete_source(self, source_id: int) -> None:
    source = await self._get_or_raise(source_id)
    await self.repo.delete(source)  # ← 明示
```

## Rationale

- **SQLAlchemy のイディオムに素直**: 既存エンティティへの `session.add` は
  公式に「冗長」とされており、立場 B の `repo.save(existing)` 呼び出しは
  実質 no-op に近い
- **Rich Model のクリーンさを汚さない**: `source.activate()` が「自分で
  完結する」と言っているのに、外側で `repo.save(source)` を呼ぶのは
  二重表現になる
- **書き忘れと規約を区別できる**: 「`save` 呼び出しが無い = 規約」と
  読者が理解できれば、Service コードは状態変更の意図だけに集中できる

## Consequences

### 想定される負担
- 読者は最初「どこで DB 変更が起きるのか」が分かりづらい。本 ADR と
  `get_session` の docstring がその知識ギャップを埋める役割を担う
- 「dependency commit が UoW を回す」という暗黙仕様を読者が理解している
  必要がある

### 派生する決定
- **`Repository.create` を `save` にリネームする計画は撤回**。
  立場 A 下では `save` の意味は実質「新規エンティティの永続化」だけに
  縮退するため、`create` のままで十分。命名の変更はコストだけが残る
- 新規エンティティの作成と削除のみ Repository を経由する。状態遷移は
  Model のドメインメソッドが担う

### 適用範囲
- 本規約は HTTP 経路の Service レイヤーが対象。Worker 経路は
  `specs/backlog/transaction-boundary-and-rich-model.md` 論点1 の
  「Worker 経路」決定に従い、Service が自分でトランザクション境界を
  切る別パターンを採る
- Rich Model 化済みのエンティティ（`NewsSource`）から本規約の対象。
  今後 Rich 化される `Keyword` 等にも同じ規約を適用する

## 参考

- 議論ドキュメント: `specs/backlog/transaction-boundary-and-rich-model.md`
- 関連 PR: news_source の activate/deactivate を Model に移すリファクタ
- 規約への入口: `backend/app/dependencies.py` の `get_session` docstring
