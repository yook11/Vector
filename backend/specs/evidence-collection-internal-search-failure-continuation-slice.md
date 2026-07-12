# Internal search failure continuation slice

## Problem

内部検索の運用障害が例外伝播し、外部根拠を取得できた mixed plan でも回答全体が失敗する。
また内部検索 metric は embedding 時点で確定するため、記事検索まで含む成否を表していない。

## Contract

内部検索 package に、回答継続可能と分類した失敗だけを表す境界例外を追加する。

```python
InternalSearchFailurePhase = Literal["query_embedding", "article_search"]

class InternalSearchError(Exception):
    phase: InternalSearchFailurePhase
```

- 自由記述messageは保持せず、`phase`だけを安全な属性として持つ。
- 元例外は `raise InternalSearchError(...) from exc` でcauseに保持する。
- query embeddingが送出した`AIProviderError`は`query_embedding`失敗へ変換する。
- article repositoryは次だけを`article_search`失敗へ変換する。
  - `sqlalchemy.exc.TimeoutError`
  - `sqlalchemy.exc.OperationalError`
  - `connection_invalidated=True`の`sqlalchemy.exc.InterfaceError`
- `SQLAlchemyError` / `DBAPIError`全体、`IntegrityError`、`ProgrammingError`、
  `DataError`、`InvalidRequestError`は変換しない。

## Behavior

- internal-onlyで`InternalSearchError`が発生した場合、次を返して回答生成を続ける。

```python
EvidenceCollectionOutcome(
    internal_hits=[],
    collection_failures=["internal_search"],
)
```

- mixed planではinternal / externalを最後まで待ち、分類済み内部失敗を値へ変換する。
- 外部検索が成功していれば、外部evidenceとtask reportを保持する。
- 外部検索が未構成なら、failure順を`["internal_search", "external_search"]`に固定する。
- internalまたはexternalの未分類例外は従来どおり伝播する。
- 両方が未分類例外なら、従来どおりinternal例外を優先して伝播する。
- collection failureを含む最終回答は`insufficient`とし、既存の
  「内部記事検索を完了できませんでした」を`missing_aspects`へ加える。

## Outcome invariants

`EvidenceCollectionOutcome`へ次のvalidatorを追加する。

- `collection_failures`は`internal_search`, `external_search`の固定順で重複しない。
- `internal_search` failureと非空`internal_hits`を同時に許可しない。
- `external_search` failureと`ExternalSearchOutcome`を同時に許可しない。
- 検索成功・0件はfailureではない。

## Observability

- `vector.agent.internal_retrieval.outcome`の識別子とresult語彙は維持する。
- overall metricの記録位置を`embed_queries()`から`search_articles()`へ移す。
- `succeeded`は最終hitsが非空、`empty`は正常完了かつhitsが空、`failed`は例外終了を表す。
- `failed`には`failure_phase=query_embedding|article_search|unknown`を付与する。
- 分類済み失敗は`internal_search_failed` warningを1回記録し、phaseとquery countだけを持たせる。
- query本文、provider message、DB statementをmetric / logへ含めない。
- limit guardによる未実行とcache lookup / save失敗の扱いは変更しない。
- live eventは`started`のままとし、`internal_search.failed`追加は後続sliceとする。

## Non-goals

- `InternalSearchOutcome`、query単位の部分成功、検索retry / fallback。
- 外部検索の例外契約変更。
- metric名、progress stage、`RetrievalPlan`の改名。
- API、DB schema、dependency、frontend変更。

## Tests

- providerの`AIProviderError`が`query_embedding`の`InternalSearchError`になる。
- repositoryの対象SQLAlchemy例外だけが`article_search`失敗になり、causeを保持する。
- `ProgrammingError`、`IntegrityError`、未分類例外は変換されず伝播する。
- internal-only / mixedの分類済み失敗が`collection_failures`になり、成功側evidenceを保持する。
- mixedの両failure順、重複、resultとの矛盾を検証する。
- 成功・0件・分類済み失敗・未分類失敗のoverall metricを検証する。
- failure metric / warningにquery本文が含まれないことを検証する。
- 最終回答が`insufficient`になり、内部検索失敗文言を含むことを検証する。

## Done

- 分類済み内部検索失敗時も、取得済み根拠から回答を完了できる。
- 想定外の実装・schema・契約不具合は隠さずagent run失敗として残る。
- 内部検索metricが記事検索まで含む実行結果を正しく表す。
- backendのlint、format、unit、integration checksがすべて通る。
