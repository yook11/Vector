# Agent source 表示契約変更 slice 仕様

## 位置付け

会話履歴 feature の `agent_message_sources` と、既存
`ResearchResponse.sources[]` の source 表示契約を揃える slice。

Slice 1 は既に実装開始済みだが、`y1_agent_history.py` / model は未コミットの
書き換え可能な実装中差分である。したがって、古い `snippet` 契約で Slice 1 を
完了させてから follow-up migration を作るのではなく、**本契約を確定して y1 migration /
model に直接畳み込んでから Slice 1 を完了する**。

履歴保存開始後に `snippet` の意味を変えると、新旧形式が混在するため、フロント結線と
履歴保存の本格利用前に source 契約を直す。

この slice は API response shape の破壊的変更を含む。実装時は `/api-contract` と
`/gen-types` を使い、DB schema に触る場合は `/migration` と Ask First を通す。

## Problem

現在の source 契約では `snippet` が internal / external で別の意味を持っている。

1. internal source の `snippet` には summary 全文が詰められている
   （`app/agent/answering/evidence.py:55`）。これは「質問に対して選んだ根拠」ではなく、
   記事にもともと付いている分析済み要約である。
2. external source の `snippet` には `ExternalSearchEvidence.claim` が詰められている
   （`app/agent/answering/evidence.py:73`）。これは evidence selector が質問に対して
   選んだ claim であり、`snippet` より `evidenceClaim` の方が API 契約として正確。

つまり `snippet` は「抜粋」「要約」「選定根拠 claim」のどれにも読める契約ノイズになっている。
source card を軽く保ちつつ、意味の違うものを同じ名前で返さないようにする。

## Evidence

- `app/agent/answering/evidence.py:55` — `snippet=hit.content.summary`（問題の箇所）。
- `app/agent/answering/evidence.py:73` — `snippet=evidence.claim`（external は実体が claim）。
- `app/agent/answering/evidence.py:81-85` — synthesizer 向け `text` は summary + key_points 結合。
  internal source から snippet を消しても **LLM が見る情報は減らない**。
  現状はむしろ prompt に summary が snippet と text で二重投入されている:
  `gemini_prompt.py:127-131`。
- `app/agent/contract.py:88-91` — AnswerSource は kind 判別 union。variant ごとに
  フィールド構成が違うのは既存構造（internal だけ article_id、external だけ url）。
- `app/agent/internal_retrieval/article_search.py:26-35` — `InternalArticleContent` は
  summary / key_points を保持するが、これは synthesizer 向け `text` の素材であり、
  final source 表示契約にそのまま出す必要はない。
- `app/agent/external_search/contract.py:257-266` — `ExternalSearchEvidence.claim` は
  evidence selector が選んだ非空 claim。
- `app/schemas/research.py` — API の SSoT。`ResearchSource` は kind 判別 union なので、
  internal / external で異なる表示 field を持てる。
- `backend/alembic/versions/y1_agent_history.py` / `backend/app/models/agent_*.py` —
  2026-07-09 時点で未コミットの実装中差分。source 契約は follow-up migration ではなく
  y1 に直接反映できる。

## 実施順

1. 本 source 契約を確定する。
2. Slice 1 の `y1_agent_history.py` / SQLAlchemy model / DB contract test に直接反映する。
3. Slice 1 を完了する。
4. API contract / worker / frontend 結線へ進む。

もし `y1_agent_history.py` が mainline に入った後に本契約を適用する状態になった場合だけ、
follow-up migration として扱う。その場合も、履歴保存が未結線で実データがないなら
data backfill は不要。

## 確定事項

- internal source の `snippet` に summary 全文を詰めるのをやめる。
- internal source は article link と title を中心にした最小契約にする。
- external source の選定根拠は `snippet` ではなく `evidenceClaim` として返す。
- internal source の `sourceName` は現状常時 `None` なので API variant から削除する。
- 出典欄は UI で重くしない。API/DB は表示に必要な snapshot を持つが、常時大量表示を前提にしない。
- `keyPoints` / `summaryPreview` は Phase 1 では持たない。将来、internal でも質問文脈依存の
  根拠表示が必要になったら、internal source にも `evidenceClaim` を追加する。

## 設計判断

1. **internal variant から snippet フィールドを削除する**（常時 null で残さない）。
   kind 判別 union なので variant 差分は既存構造どおり。回答には内部記事が
   `articleId` と `title` で紐づくため、「何の記事か」はそれで表現する。
2. **external variant は `snippet` を `evidenceClaim` に rename する**。
   候補検索 provider の `snippet` とは別物で、回答 source として表示するのは
   selector が選んだ claim だからである。
3. **internal variant から `sourceName` も削除する**。
   現状 `evidence.py` で常時 `None` 固定で、同じく契約ノイズになる。将来 source 名を
   表示する consumer と素材が揃った時点で nullable field として追加する。
4. **internal に `keyPoints` / `summaryPreview` は追加しない**。
   出典欄を重くしない方針と、consumer がまだない表示データを永続化しない方針に合わせる。
5. **将来の internal 根拠表示は `evidenceClaim` 追加で扱う**。
   internal retrieval だけでは質問文脈依存 claim は存在しない。将来 claim extractor /
   reranker を入れた時点で、internal variant に `evidenceClaim` を追加する。
6. **history read の `articleId` は nullable にする**。
   live 応答は回答直後なので internal source の `articleId` は required のままでよい。
   ただし履歴行は `analyzed_article_id ON DELETE SET NULL` により削除後 NULL が正当状態になる。

## API 契約

`ResearchSource` は kind 判別 union のまま維持する。

```yaml
ResearchInternalArticleSource:
  kind: internal_article
  sourceRef: string
  articleId: integer
  title: string
  publishedAt: datetime | null

ResearchExternalUrlSource:
  kind: external_url
  sourceRef: string
  url: SafeUrl
  title: string
  sourceName: string | null
  publishedAt: datetime | null
  evidenceClaim: string        # required。selector が選んだ claim
```

欠損の意味:

- internal に `snippet` / `evidenceClaim` / `sourceName` は存在しない。
- external に internal-only の `articleId` は存在しない。
- `evidenceClaim` は external source が回答に対して支える主張であり、検索候補 snippet ではない。

履歴 read API では、internal source の `articleId` だけ nullable にする:

```yaml
ResearchHistoryInternalArticleSource:
  kind: internal_article
  sourceRef: string
  articleId: integer | null   # analyzed article 削除後は null
  title: string
  publishedAt: datetime | null
```

これは「履歴表示の snapshot は再現するが、削除済み内部記事へのリンク ID は復元不能」
という意味で、`title` / `sourceRef` / `publishedAt` は snapshot として残る。

## 変更ファイル

```
backend/app/agent/contract.py            InternalArticleSource: snippet / source_name 削除
                                         ExternalUrlSource: snippet を evidence_claim に rename
backend/app/agent/answering/evidence.py  _normalize_internal_hit の写像変更
                                         _normalize_external_evidence の写像変更
backend/app/agent/answering/ai/gemini_prompt.py
                                         source.snippet 参照を variant-aware に変更し、
                                         external は claim ラベルで render
backend/app/schemas/research.py          ResearchInternalArticleSource: snippet / source_name 削除
                                         ResearchExternalUrlSource: evidence_claim 追加、snippet 削除
backend/specs/agent-history-schema-slice.md
                                         y1 DDL を snippet なし / evidence_claim ありに更新
backend/tests/                           evidence normalization / router 写像 / prompt レンダリングの既存テスト追従
frontend                                 /gen-types のみ（UI 実装は結線 slice の責務）
```

candidate search 側の `ExternalSearchCandidate.snippet` は変更しない。これは検索 provider 由来の
候補抜粋であり、final answer source の `evidenceClaim` とは別の契約。

## 履歴 schema への反映

Slice 1 の完了状態に応じて、`agent_message_sources` を次の表示契約に合わせる。

推奨 column:

```text
kind                  varchar(32) not null
source_ref            text not null
ordinal               integer not null
analyzed_article_id   integer null
url                   text null
title                 text not null
source_name           text null
published_at          timestamptz null
evidence_claim        text null
```

DB invariant の目安:

- `kind='internal_article'` のとき `url is null`、`source_name is null`、
  `evidence_claim is null`。
  `analyzed_article_id` は insert 時は app-layer で非 NULL を保証するが、記事削除後は
  `ON DELETE SET NULL` により NULL が正当な永続状態になるため DB CHECK にはしない。
- `kind='external_url'` のとき `url is not null`、`analyzed_article_id is null`、
  `evidence_claim` は非空。URL は SafeUrl の app-layer 検証に加えて DB で scheme / length の
  backstop を置く。

2026-07-09 時点では Slice 1 migration は未コミットのため、`snippet` を作らず
`evidence_claim` を y1 に直接入れる。既に実データが存在する状態で契約変更する場合だけ、
external row の `snippet` を `evidence_claim` に移し、internal row の `snippet` は破棄する
方針を Ask First で確認する。

## Invariants

- 回答生成（synthesizer prompt の text 内容）を劣化させない。
- external final source の選定根拠は `evidenceClaim` として非空で返す。
- internal source に質問文脈依存の claim が無い場合、それを擬装しない。
- internal source の `sourceName` は素材と consumer が出るまで契約に出さない。
- internal source の `analyzedArticleId` / `articleId` は削除後 NULL が正当状態。
- contract の `AnswerQuestionResult` validator（引用照合・provenance 検証）に触れない。
- `sourceRef` と回答本文の `[[sourceRef]]` 対応は維持する。
- API の SSoT は Pydantic schema。実装後に `/gen-types` する。

## Non-goals

- internal の質問文脈依存スニペット生成（claim extractor / reranker。必要になれば
  internal variant に `evidenceClaim` を追加する）。
- internal source の `keyPoints` / `summaryPreview` 追加。
- internal source の `sourceName` 追加。
- frontend UI 実装。
- external search provider candidate の `snippet` rename。
- 回答本文・citation marker 形式の変更。

## Tests

1. internal hit の写像: final source に snippet / evidenceClaim / sourceName が存在しない。
2. external evidence の写像: `evidence.claim` が `ExternalUrlSource.evidence_claim` に入る。
3. prompt レンダリング: internal item に snippet / source_name 行が出ない。external item には
   `claim:` ラベルで evidenceClaim が出る。
4. OpenAPI: ResearchInternalArticleSource / ResearchExternalUrlSource の shape 変更が生成型に届く。
5. 履歴 mapper がある場合: live source と履歴 source の違い（history articleId nullable）を
   schema と mapper で表現できる。
6. DB contract: analyzed article 削除後、internal source の `analyzed_article_id` は NULL になり、
   source row と snapshot title は残る。

## 検証

- `/gen-types`
- `/check`

dev は egress 無しのため実 LLM E2E は対象外（既存 slice と同じ制約）。

## Done

- `ResearchResponse.sources[]` から曖昧な `snippet` が消える。
- internal source は `articleId` / `title` を中心にした最小契約で返る。
- internal source から常時 null の `sourceName` が消える。
- external source が `evidenceClaim` で返る。
- Slice 1 の y1 schema が `snippet` なし / `evidence_claim` ありの契約に直接更新される。
- 履歴 read では削除済み internal article の `articleId` nullable が契約に現れる。
- `/gen-types` 済み。既存 suite green。
