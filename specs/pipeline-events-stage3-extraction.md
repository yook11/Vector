# pipeline_events Stage 3 (Extraction) — 実装仕様

Stage 3 (extraction) の error taxonomy / audit 永続化 / Task 層 dispatch の
実装確定仕様。共通基盤 (Layer 1 marker / Layer 2-A 9 種 / DB schema /
AuditRepository パターン) は `pipeline-events-error-taxonomy.md` を参照する。

ステータス: **PR3.5-c で確定** (PR #418)、**2026-05-15 リファクタで Stage 4/5 と
同じ ACL 方式に統一** (foundation marker 多重継承 → Stage 3 専用 Layer 1 marker
3 軸 + ACL tuple)。本仕様は確定後の SSoT。

履歴:
- 2026-05-08 切出 (`pipeline-events-error-taxonomy.md` 五改訂時点の Stage 3
  記述を本ファイルに分離。共通基盤は foundation spec を参照)
- 2026-05-15 ACL 方式統一 (Stage 4/5 と対称化)。foundation marker
  (`RetryableError` / `NonRetryableDropArticle` / `NonRetryableKeepArticle`) を
  撤去し、Stage 3 専用 Layer 1 marker `ExtractionRecoverableError` /
  `ExtractionTerminalKeepError` / `ExtractionTerminalDropError` の 3 軸 +
  `map_provider_to_extraction` ACL + 3 tuple (`EXTRACTION_RECOVERABLE_*` /
  `EXTRACTION_TERMINAL_KEEP_*` / `EXTRACTION_TERMINAL_DROP_*`) に切替。
  `INLINE_RETRY` ClassVar を廃止し taskiq retry 上限後の cron 救済に統一。
  `ExtractionDomainError` を `ExtractionError` に rename。本 spec 下記の
  「`NonRetryable*` を継承」「`INLINE_RETRY=True/False`」「`match exc: case
  RetryableError():`」記述は **歴史的**。現行実装は
  `backend/app/analysis/extraction/errors.py` / `failure_handling.py` /
  `audit_repository.py` を参照。設計差分:
  - Stage 3 marker は **3 軸** (Stage 4/5 は 2 軸): article DELETE / Keep /
    Recoverable
  - ACL `map_provider_to_extraction` は **3 tuple** を持つ (DROP / KEEP /
    RECOVERABLE) ことを除き Stage 4/5 と同形
  - Stage 3 boundary は `ExtractionService.execute` と
    `ReExtractionService._extract_once_mapped` の 2 箇所 (retry loop の
    sibling-except trap 回避のため再抽出側は内部 helper を分離)
  - `_code_of` は `getattr(exc, "code", None)` (Stage 3 marker の instance
    attr)、`error_chain` は `observability.recording._extract_error_chain` で
    `__cause__` chain を保持し ACL の `raise from` で詰めた元 provider error
    まで audit row に焼く

---

## Stage 3 が扱う失敗の全体像

Stage 3 は記事原文を Gemini に渡して翻訳タイトル / 事実要約 / entity 群を
抽出する。失敗の発生源は 3 階層:

| 階層 | 出所 | 例 |
|---|---|---|
| **provider 由来** (Layer 2-A、共通) | Gemini SDK / network | API key 不正、context 超過、5xx、policy block |
| **Stage 3 工程由来** (Layer 2-B) | response 解釈 | `response.parsed` が `ExtractionResult` でない (format 違反) |
| **想定外** (catch-all) | bug / 仕様変更 | SDK の新例外、Python `RuntimeError` |

各階層を Layer 1 marker (`NonRetryableDropArticle` / `NonRetryableKeepArticle`
/ `RetryableError` / catch-all `Exception`) に多重継承で紐付け、Task 層の
4 except で dispatch する。

---

## Error Survey — Gemini SDK の実例

`google-genai` SDK が Stage 3 で raise しうる例外と、それを Layer 2 に翻訳
する根拠。`app/analysis/extraction/extractor/gemini.py:_translate_error` の
SSoT。

### APIError (`google.genai.errors.APIError`) の status 別対応

| `exc.status` | 翻訳先 (Layer 2) | Layer 1 | 根拠 |
|---|---|---|---|
| `UNAUTHENTICATED` / `PERMISSION_DENIED` / `FAILED_PRECONDITION` / `NOT_FOUND` | `AIProviderConfigurationError` | NonRetryableKeepArticle | API key / project / model 名の設定不正、コード/設定を直すまで解消しない |
| `INVALID_ARGUMENT` / `DEADLINE_EXCEEDED` (※ context length パターン) | `AIProviderInputRejectedError` | **NonRetryableDropArticle** | 本文長が context window を超えた、retry でも変わらない、記事 DELETE |
| `INVALID_ARGUMENT` / `DEADLINE_EXCEEDED` (※ それ以外) | `AIProviderRequestInvalidError` | NonRetryableKeepArticle | request 構築の bug、コード/SDK 修正で解消 |
| `RESOURCE_EXHAUSTED` | `AIProviderRateLimitedError` | RetryableError (INLINE_RETRY=False) | Gemini 短期レート、cron 再投入で解消。RPD は taskiq RateLimiter が事前カット済 |
| その他 status | `_translate_error` は exc を return | catch-all (UNKNOWN) | 翻訳不可、`_call_once` が bare re-raise → Task 層 catch-all で UNKNOWN ラベル |
| message に `"reported as leaked"` が含まれる | `AIProviderConfigurationError` | NonRetryableKeepArticle | API key 漏洩検知、key rotation で解消 |

### ServerError (`google.genai.errors.ServerError`、APIError の派生)

| 条件 | 翻訳先 | Layer 1 |
|---|---|---|
| 上記 status 分岐に該当しない 5xx 系 | `AIProviderServiceUnavailableError` | RetryableError (INLINE_RETRY=True) |

### finish_reason ベースの policy block

`response.candidates[0].finish_reason` が以下のいずれかなら、provider 自体は
応答 (200) を返したが内容が policy 違反として遮断された状態。

```python
_POLICY_BLOCKED_FINISH_REASONS = frozenset(
    {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}
)
```

| 翻訳先 | Layer 1 | 根拠 |
|---|---|---|
| `AIProviderOutputBlockedError` | **NonRetryableDropArticle** | retry / 別モデルでも通らないと確信できる、ニュース配信したくない内容と一致 (政治暴力 / 性的 / ヘイト等) |

### response.parsed が `ExtractionResult` でない (format 違反)

provider は応答したが Pydantic schema として消化不可なケース。

| 翻訳先 | Layer 1 | 根拠 |
|---|---|---|
| `ExtractionResponseInvalidError` (Layer 2-B) | RetryableError (INLINE_RETRY=True) | AI モデルの揺らぎ (構造化出力でも稀に schema を外す)、retry 救済が現実的に効く |

### Pydantic ValidationError

Gemini が一見成功した JSON を返したが Pydantic validate で reject されたケース
(`response_schema` 設定下でも稀に発生)。

| 翻訳先 | Layer 1 |
|---|---|
| `ExtractionResponseInvalidError` (Layer 2-B) | RetryableError (INLINE_RETRY=True) |

### Python 標準例外

| 例外 | 翻訳先 | Layer 1 |
|---|---|---|
| `TimeoutError` / `ConnectionError` / `OSError` | `AIProviderNetworkError` | RetryableError (INLINE_RETRY=True) |
| 上記以外の `Exception` | exc を return | catch-all (UNKNOWN) |

---

## Layer 2-B (Stage 3 固有) の確定形

```python
# app/analysis/errors/extraction.py

class ExtractionDomainError(Exception):
    """Stage 3 extraction 固有のドメインエラーの基底。"""


class ExtractionResponseInvalidError(ExtractionDomainError, RetryableError):
    """provider は応答したが Stage 3 schema として消化不可。

    - Pydantic ValidationError で reject
    - response.parsed が ExtractionResult でない
    - JSON parse 不能、必須 field 抜け、entity 整合性違反 等
    """
    CODE: ClassVar[str] = "extraction_response_invalid"
    INLINE_RETRY: ClassVar[bool] = True
```

Stage 3 で Layer 2-B を 1 種にとどめた理由 (slug resolution 等の概念は Stage 3
には存在しない、entity 整合性違反も `ExtractionResponseInvalidError` で表現
できる粒度のため別型を作らない)。

---

## Outcome 確定形 — 成功種別のみ

```python
# app/analysis/extraction/service.py

@dataclass(frozen=True, slots=True)
class ExtractedOutcome(SuccessOutcome):
    """signal として成功、Stage 4 (classification) に chain する。"""
    CODE: ClassVar[str] = "extracted"
    extraction: Extraction


@dataclass(frozen=True, slots=True)
class NoiseOutcome(SuccessOutcome):
    """noise として成功、extraction_noises に永続化済、chain しない。"""
    CODE: ClassVar[str] = "extracted_as_noise"


# 失敗は全て raise (Outcome union に入れない、foundation spec §原則 4)
ExtractionOutcome = ExtractedOutcome | NoiseOutcome
```

`SuccessOutcome` 継承 + `CODE` ClassVar pin により、成功 audit でも
`category=success` + `code=type(outcome).CODE` を type SSoT から焼ける。

**廃止** (PR3.5-c で削除):

| 旧 | 新 |
|---|---|
| `InvalidInputError` (Service catch → Outcome 変換) | 廃止。AI 真の処理拒否は `AIProviderInputRejectedError` (Layer 2-A、DROP)、format 違反は `ExtractionResponseInvalidError` (Layer 2-B、RETRYABLE) に分離 |
| `InvalidInputOutcome` (失敗を Outcome に混ぜていた) | 廃止 (失敗は raise) |
| `ExtractionOutcome = ExtractedOutcome \| NoiseOutcome \| InvalidInputOutcome` | `ExtractionOutcome = ExtractedOutcome \| NoiseOutcome` |
| `ExtractionPolicyBlockedError` / `ExtractionInputTooLargeError` (PR #410 で追加) | 廃止。Layer 2-A の `AIProviderOutputBlockedError` / `AIProviderInputRejectedError` に吸収 |

---

## ExtractionAuditRepository — Stage 3 監査 row の shape SSoT

`app/analysis/extraction/audit_repository.py`。Service / Task は
**`PipelineEventRepository.append()` を直接呼ばない**。本 class の semantic
method を呼ぶだけで、`ExtractionPayload` の組み立て・`error_chain` の FQN
構築・`category` / `code` の決定を一切知らない。

tx 境界は呼出側が握る (本 class は `commit` を呼ばない)。

### 4 semantic methods

| method | 用途 | category | code | 呼ばれる場所 |
|---|---|---|---|---|
| `append_extracted(*, ready, envelope, code)` | signal 成功 | `success` | caller 渡し (`ExtractedOutcome.CODE`) | Service `_persist_signal` 内、業務 INSERT と同 tx |
| `append_noise(*, ready, envelope, code)` | noise 成功 | `success` | caller 渡し (`NoiseOutcome.CODE`) | Service `_persist_noise` 内、同 tx |
| `append_drop_article(*, article_id, original_content, code, exc)` | 内容起因 Permanent | `non_retryable_drop_article` | caller 渡し (`type(exc).CODE`) | Service `mark_article_unprocessable` 内、article DELETE と同 tx |
| `append_failure(*, ready, exc, attempt)` | KEEP / Retryable / catch-all | exc から自動導出 | exc から自動導出 | Task 層 `record_extraction_failure` 経由、別 session 別 tx |

### 内部実装

```python
class ExtractionAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = PipelineEventRepository(session)  # generic SQL を委譲

    async def append_failure(
        self,
        *,
        ready: ReadyForExtraction,
        exc: BaseException,
        attempt: int,
    ) -> None:
        # category / code は exc から自動導出 (Stage 3 SSoT)
        category = self._category_of(exc)
        code = self._code_of(exc)
        ...
        await self._events.append(
            stage=Stage.EXTRACTION,
            event_type=EventType.FAILED,
            outcome_code=code,
            payload=payload,
            article_id=ready.article_id,
            attempt=attempt,
            error_class=_fqn(exc),
            category=category,
            code=code,
        )

    @staticmethod
    def _category_of(exc: BaseException) -> Layer1Category:
        if isinstance(exc, NonRetryableDropArticle):
            return Layer1Category.NON_RETRYABLE_DROP_ARTICLE
        if isinstance(exc, NonRetryableKeepArticle):
            return Layer1Category.NON_RETRYABLE_KEEP_ARTICLE
        if isinstance(exc, RetryableError):
            return Layer1Category.RETRYABLE
        return Layer1Category.UNKNOWN

    @staticmethod
    def _code_of(exc: BaseException) -> str:
        code = getattr(type(exc), "CODE", None)
        return code if isinstance(code, str) and code else "unexpected_error"
```

### 設計判断

- **`append_extracted` / `append_noise` を別 method に分離**: 内容は同一だが
  意味 (signal vs noise) が違うため、caller が outcome 種別ごとに呼び分け
  できるようにする
- **`append_drop_article` は `code: str` を caller から受ける**: Service の
  `mark_article_unprocessable` signature と一致 (caller が `type(exc).CODE`
  を渡す)
- **`append_failure` のみ exc から自動導出**: Task 層 4 marker dispatch の
  共通化、isinstance 分岐 + `CODE` 抽出を 1 箇所に集約
- **`PipelineEventRepository` を compose**: generic な append SQL は
  generic repo に委譲、本 class は Stage 3 固有の payload shape /
  category / code 決定だけを担う
- **commit しない**: tx 境界は caller (Service / `record_extraction_failure`)
  が握る、同一 tx 必須経路 (signal / noise / drop) と別 tx 経路 (failure-only)
  を caller 側で出し分ける

---

## record_extraction_failure — Task 層 application helper

`app/analysis/extraction/failure_recording.py`。業務 tx が rollback された
後に **別 session で別 tx** として audit を焼く。

```python
async def record_extraction_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ready: ReadyForExtraction,
    exc: BaseException,
    attempt: int,
) -> None:
    """Stage 3 failure を pipeline_events に焼付ける (Task 層から呼ぶ)。

    audit INSERT 自体に失敗した場合は exception を吞んで warning ログを
    残す (audit 失敗で業務 task まで死なせない)。
    """
    try:
        async with session_factory() as session:
            await ExtractionAuditRepository(session).append_failure(
                ready=ready, exc=exc, attempt=attempt,
            )
            await session.commit()
    except Exception as audit_exc:
        logger.exception("extraction_failure_audit_dropped", ...)
```

**設計判断**:

- `Stage.EXTRACTION` の hardcode は Stage 3 専用 helper であることを名前
  (`record_extraction_failure`) で表現済
- audit INSERT 失敗を吞む方針は `_record_failure_event` (recording.py) と
  同じ。`_record_failure_event` 自体は Stage 3 経路では使わない (collection
  系 / Stage 4/5 PR3.5-d 移行前用の generic helper として残置)

---

## Service 内 — 成功経路と DROP 経路

### `_persist_signal` / `_persist_noise` (成功経路)

```python
async def _persist_signal(
    self,
    ready: ReadyForExtraction,
    envelope: ExtractionCall,
    ai_model: str,
) -> ExtractedOutcome:
    async with self._session_factory() as session:
        repo = ExtractionRepository(session)
        saved = await repo.save(envelope.result, ...)

        if saved is None:
            # race 敗北 — 勝者を読み戻して合流 (audit は勝者側で焼かれる)
            ...
            return ExtractedOutcome(extraction=...)

        # 同 tx に audit 焼付 — shape は audit_repository に閉じ込め
        await ExtractionAuditRepository(session).append_extracted(
            ready=ready, envelope=envelope, code=ExtractedOutcome.CODE,
        )
        await session.commit()
        return ExtractedOutcome(extraction=saved)
```

`_persist_noise` も同型 (`append_noise` / `NoiseOutcome.CODE`)。

### `mark_article_unprocessable` (DROP 経路)

```python
async def mark_article_unprocessable(
    self,
    article_id: int,
    original_content: str,
    *,
    code: str,
    exc: BaseException,
) -> None:
    """内容起因 Permanent failure を 1 tx で焼付け + 記事 DELETE する。

    順序: audit INSERT 先、DELETE 後 — A 級保険を最大化し、source_id の
    自動逆引きが Article 存在中に確定するように。FK は ondelete=SET NULL
    設定済 (pipeline_events.article_id) のため DELETE 後も audit 行は残り、
    source_id で起点ソースを追跡可能。
    """
    async with self._session_factory() as session:
        # 1) audit (shape は audit_repository に閉じ込め)
        await ExtractionAuditRepository(session).append_drop_article(
            article_id=article_id,
            original_content=original_content,
            code=code, exc=exc,
        )
        # 2) article DELETE (CASCADE で関連 row、SET NULL で audit.article_id)
        deleted = await ArticleRepository(session).delete_by_id(article_id)
        await session.commit()
```

---

## Task 層 — extract_content の 4 except dispatch

```python
@broker_analysis.task(
    task_name="extract_content", timeout=180, max_retries=1, retry_on_error=True,
)
async def extract_content(
    ready: ReadyForExtraction, ctx: Context = TaskiqDepends(),
) -> None:
    session_factory = ctx.state.session_factory
    extractor: BaseExtractor = ctx.state.extractor
    ...

    svc = ExtractionService(session_factory)
    attempt = int(ctx.message.labels.get("retry_count", 0)) + 1
    try:
        result = await svc.execute(ready, extractor)
    except NonRetryableDropArticle as exc:
        await svc.mark_article_unprocessable(
            ready.article_id, ready.original_content,
            code=getattr(type(exc), "CODE", "ai_error_unknown_drop"),
            exc=exc,
        )
        return
    except NonRetryableKeepArticle as exc:
        await record_extraction_failure(
            session_factory, ready=ready, exc=exc, attempt=attempt,
        )
        return
    except RetryableError as exc:
        if type(exc).INLINE_RETRY and not is_last_attempt(ctx):
            raise  # taskiq 即時 retry
        await record_extraction_failure(
            session_factory, ready=ready, exc=exc, attempt=attempt,
        )
        return
    except Exception as exc:
        await record_extraction_failure(
            session_factory, ready=ready, exc=exc, attempt=attempt,
        )
        return

    # 成功経路 (Outcome は成功種別のみ、Service が同 tx で audit 焼付済)
    if isinstance(result, ExtractedOutcome):
        # Stage 4 へ chain
        ...
    elif isinstance(result, NoiseOutcome):
        logger.info("extract_content_noise", article_id=ready.article_id)
```

---

## extractor — `_translate_error` と `_call_once`

### `GeminiExtractor._translate_error`

`app/analysis/extraction/extractor/gemini.py`。SDK 例外を Layer 2-A or
Layer 2-B に翻訳。翻訳不可は exc をそのまま return (`_call_once` 側で
bare re-raise → Task 層 catch-all)。

詳細な status 別マッピングは §Error Survey 参照。

### `_call_once` の raw re-raise guard (`base.py`)

```python
async def _call_once(self, prompt: str) -> ExtractionCall:
    try:
        envelope = await self._call_api(prompt)
        return envelope
    except (AIProviderError, ExtractionDomainError):
        # 既に Layer 2 に翻訳済 (_call_api 内で raise された)
        raise
    except Exception as exc:
        translated = self._translate_error(exc)
        if translated is exc:
            raise  # bare re-raise — 自己 chain を避け、Task 層 UNKNOWN へ
        raise translated from exc
```

`if translated is exc: raise` ガードは `__cause__` 自己参照 chain を避け、
Sentry / structlog の stacktrace serializer で無限ループを起こさない
ための防御 (PR3.5-c 設計レビューで追加)。

---

## 実装ファイル一覧 (PR3.5-c 完了時点)

| パス | 役割 |
|---|---|
| `app/analysis/errors/provider.py` | Layer 2-A 9 種 (foundation で定義) |
| `app/analysis/errors/extraction.py` | Layer 2-B (`ExtractionDomainError` / `ExtractionResponseInvalidError`) |
| `app/analysis/extraction/extractor/base.py` | `BaseExtractor` (`_call_once` raw re-raise guard) |
| `app/analysis/extraction/extractor/gemini.py` | `GeminiExtractor` (`_translate_error` / `_call_api`) |
| `app/analysis/extraction/audit.py` | `base_extraction_payload_fields` (audit_repository 内部 helper) |
| `app/analysis/extraction/audit_repository.py` | `ExtractionAuditRepository` (4 semantic method) |
| `app/analysis/extraction/failure_recording.py` | `record_extraction_failure` |
| `app/analysis/extraction/service.py` | `ExtractionService` (audit_repository 経由) |
| `app/analysis/tasks.py` | `extract_content` (4 except dispatch) |

---

## 関連 PR

- **PR #410**: Stage 3 監査統合 + DELETE 機構 (旧階層のまま 10 except 節)
- **PR3.5-a** (PR #417 同梱): Layer 1 marker / Layer 2-A 9 種 / Layer 2-B Stage 3 を新設 (型のみ)
- **PR3.5-b** (PR #417 同梱): `pipeline_events.category` / `code` 列追加
- **PR #418 (PR3.5-c)**: Stage 3 を新型例外に切替 + 監査永続化を Repository に集約 (本仕様の確定 PR)

---

## 関連仕様

- `pipeline-events-error-taxonomy.md` — Foundation (Layer 1/2 型システム、
  DB schema、AuditRepository パターン規約、PR ロードマップ)
- `pipeline-events-stage4-classification.md` — Stage 4 (PR3.5-d 着手時に作成)
- `pipeline-events-stage5-embedding.md` — Stage 5 (PR3.5-e 着手時に作成)
