# Evidence collection failure continuation slice

## Problem

内部検索が分類可能な運用障害で失敗すると、外部根拠を取得できていても回答全体が失敗する。
また `unmet_requirements` は、実際に表す「失敗した収集経路」が名前から読み取りにくい。

## Evidence

- `EvidenceCollectionService` は internal / external を並列実行後、片方の例外を再送出する。
- `EvidenceCollectionOutcome.unmet_requirements` は外部検索未構成時だけ生成される。
- 内部検索の metric は embedding 完了時点で記録され、記事検索失敗を表せない。
- 最終回答は収集失敗を `missing_aspects` へ変換できる既存経路を持つ。

## Contract

```python
EvidenceCollectionFailure = Literal["internal_search", "external_search"]

class EvidenceCollectionOutcome(BaseModel):
    internal_hits: list[InternalArticleSearchHit]
    external_search: ExternalSearchOutcome | None
    collection_failures: list[EvidenceCollectionFailure]

class InternalSearchError(Exception):
    """回答継続可能と分類された内部検索の運用失敗。"""
```

- `UnmetRequirement` と `unmet_requirements` を、それぞれ
  `EvidenceCollectionFailure` と `collection_failures` へ置き換える。
- failure の順序は `internal_search`, `external_search` の固定順とし、重複を許可しない。
- `InternalSearchError` は既知の provider / repository 運用失敗だけに使用し、元例外を cause として保持する。
- 未分類例外、契約違反、実装不具合は `InternalSearchError` へ変換しない。

## Behavior

| 状態 | 結果 |
|---|---|
| 内部検索成功・0件 | `internal_hits=[]`, failure なし |
| 内部検索失敗 | `internal_hits=[]`, `collection_failures=["internal_search"]` |
| 外部検索未実行 | `external_search=None`, `collection_failures=["external_search"]` |
| mixed の片方が失敗 | 成功側の根拠を保持して回答生成を続行 |
| 分類されていない例外 | 従来どおり伝播して agent run を失敗させる |

- collection failure が1件以上あれば、最終回答は `insufficient` とする。
- 既存のユーザー向け文言を維持し、失敗した収集経路を `missing_aspects` に加える。
- 内部検索全体の `succeeded / empty / failed` metric は `search_articles()` 境界で記録する。
- cache lookup / save failure は従来どおり best-effort とし、collection failure に含めない。

## Invariants

- 検索成功・0件と検索処理失敗を区別する。
- `internal_search` failure と非空の `internal_hits` は同時に返さない。
- `external_search` failure と `ExternalSearchOutcome` は同時に返さない。
- 取得済み evidence、citation、外部 task report の契約を変更しない。

## Non-goals

- `InternalSearchOutcome` の追加。
- query 単位の部分成功。
- `internal_search.failed` live event と公開 API schema の追加。
- `RetrievalPlan`、`retrieval_mode`、progress stage、既存 metric 識別子の一括改名。
- DB schema、dependency、frontend 表示の変更。

## Tests

- internal-only の分類済み失敗が internal collection failure になる。
- mixed の内部失敗時に外部 evidence を保持し、回答を続行する。
- mixed の外部未実行時に内部 hits を保持する。
- 検索成功・0件は failure にならない。
- 未分類例外は伝播する。
- outcome の failure 重複・結果との矛盾を validator が拒否する。
- metric が記事検索を含む内部検索全体の成功・空・失敗を表す。

## Done

- agent 内部契約が `collection_failures` に統一されている。
- 分類済み内部検索失敗時も、取得済み根拠から `insufficient` 回答を生成できる。
- 既存の API / DB / frontend contract を変更せず、backend の全 check が通る。
