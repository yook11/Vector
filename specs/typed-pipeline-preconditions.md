# 型による operation precondition 保証 — Pipeline 全体の再設計

作成: 2026-04-28
ブランチ: 別ブランチで実施予定（spec 確定後）

---

## 1. 設計の北極星

### 1.1 型 = operation の前提条件 (ReadyForX)

このアプリケーションには分析を行うために満たさなければいけない条件がある。それを **型として保証する**ことで、その型を受け取って処理するだけで、operation が実行できることを構造的に保証する。

**型 = データの形ではなく、operation の前提条件**

- 各 operation には事前条件がある (例: 「分類するには extraction 完了 + 未分類 + 未却下」)
- 事前条件を満たした状態を `ReadyForX` 型で表現する
- 型のフィールドは **その operation に必要な値だけ**。永続化単位や aggregate 境界とは無関係
- ReadyForX は Aggregate Entity の projection / view ではなく、operation 用に新規構築された独立型

### 1.2 既存型レイヤとの関係 — 3 軸の並立

Vector のアプリケーションには 3 軸の型レイヤがあり、それぞれ別の不変条件を守る。本リファクタは ReadyForX を加えるが、既存レイヤを置き換えない。

| レイヤ | 役割 / 守る不変条件 | 例 |
|---|---|---|
| **境界 parse 型** | 不確定な外部入力 (AI response, API input) を variant に確定 | `Classified \| OutOfScope` (AI レスポンス), 各 Pydantic schema |
| **precondition 型** (新) | operation 実行前提を構造保証 | `ReadyForClassification`, `ReadyForEmbedding` |
| **不変条件型** | field / aggregate / Draft の invariant 担保 | `TopicName` (VO), `Analysis` (Aggregate), `AnalysisDraft` (Draft) |

3 軸は並立し、相互に置換できない。たとえば classification operation では:

- 入力境界に `Classified | OutOfScope` (AI 返値の parse)
- 入力 precondition に `ReadyForClassification`
- 永続化アグリゲートに `Analysis` / `Rejection` (不変条件保証)

### 1.3 Service が削減する分岐は precondition 分岐のみ

「Service は条件分岐ゼロで execute」という表現には注意が必要。削減対象は **precondition 由来の if 分岐** のみ。

| 分岐の種類 | 例 | 処遇 |
|---|---|---|
| precondition 分岐 | `if extraction is None`, `if existing_analysis is not None` | **削減**。ReadyForX で構造保証され消える |
| 境界 parse 由来の tagged-union match | `match response: case Classified() / case OutOfScope()` | **維持**。AI 出力は型で縛れないため境界 parse 後の variant dispatch が必須 |

AI / 外部 IO の出力は型で完全に縛れない。boundary で parse + validate して variant に確定させ、それを tagged-union 型で保証することで、出力の不確定性を型に閉じ込める。`match` 削除 = 「parse した variant をまた捨てる」= boundary を引いた意味がなくなる。

→ **Service の precondition 分岐はゼロ**になる。tagged-union match は business state 移管を表現する正当な dispatch として残る。

## 2. 型化前後の違い (concrete example)

### 型化前 (現状の `ClassificationService.execute`)

```python
async def execute(self, article_id: int, classifier) -> ClassificationOutcome:
    extraction = await extraction_repo.find_by_article_id(article_id)
    if extraction is None:
        return SkippedOutcome(reason="extraction_not_found")  # 分岐 1
    
    existing_analysis = await analysis_repo.find_by_extraction_id(extraction.id)
    if existing_analysis is not None:
        return AlreadyClassifiedOutcome(analysis=existing_analysis)  # 分岐 2
    
    existing_rejection = await rejection_repo.find_by_extraction_id(extraction.id)
    if existing_rejection is not None:
        return AlreadyRejectedOutcome(rejection=existing_rejection)  # 分岐 3
    
    response = await classifier.classify(...)
    match response: ...  # 分岐 4 (これだけが型 dispatch)
```

Service が「実行可能か」を 3 つの if で判定し、各分岐に対応する `Outcome` 型が増殖している。

### 型化後

```python
async def execute(
    self,
    ready: ReadyForClassification,
    classifier: BaseClassifier,
) -> ClassifiedOutcome | RejectedOutcome:
    # precondition は型で保証されている (precondition 分岐ゼロ)。
    # 下の match は AI レスポンス境界 parse 結果の dispatch、業務 state 移管として維持 (§1.3)
    response = await classifier.classify(
        title_ja=ready.translated_title,
        summary_ja=ready.summary,
    )
    match response:
        case Classified(): ...
        case OutOfScope(): ...
```

Outcome は 5 種類 → 2 種類に縮退する (`Skipped` / `Already*` は消える)。

## 3. 責任分担

### 3.1 各層の責任 (Pattern A')

| 層 | 責任 | 失敗時の表現 |
|---|---|---|
| **Domain (Ready 型)** | precondition rule の唯一所有 + Ready 型の不変条件保証。`try_advance_from(upstream, repo_protocols...)` が gatekeeper | 不変条件違反 → `ValueError`。precondition 未充足 → `None` |
| **Repository** | 個別 record の fetch + cheap `exists_for_*` 判定 (raw data 層) | 該当なし → `None` / `False` |
| **Service** | Ready 型を受け取り execute。precondition validation も Ready 構築も持たない | 実行時 failure のみ throw (既存の `ProviderError` / `IntegrityError` 等) |
| **Task** | upstream Stage 完了後に `Ready.try_advance_from(...)` を呼ぶ → 成功すれば次 Stage を `kiq`。precondition rule は知らない | `None` 返却時は何もしない (= 自然に止まる)、`ValueError` は dead letter + alert |

### 3.2 設計判断: 「次 Stage に進めるか」の判定は Domain (Ready 型) が所有 (β'' 案)

選択肢:

- α: Repository の SQL WHERE 句で判定 (rule が SQL に埋まる)
- β: Domain smart constructor `try_construct(entity_a, entity_b, ...)` を Task が呼ぶ (Task が rule を知る)
- **β'' (採用): Ready 型の `try_advance_from(upstream, repo_protocol...)` が rule を所有、Task は呼ぶだけ**
- γ: Application Service を新設 (Vector 規模で layer 過多)

β'' を採用する理由:

1. **rule の単一所有**: 「Stage X に進める条件」が `ReadyForX` に閉じ、Task や Service に散らない (`feedback_responsibility_by_purpose.md` と整合)
2. **Stage 間 coupling 削減**: 上流 Stage の Task は「Ready 型に問い合わせる」だけ。下流 Stage の rule を知らなくていい
3. **Domain-led**: precondition rule は business 知識であり、Domain が持つのが自然
4. **構造保証**: `feedback_structural_guarantee.md` (不変条件は DB 制約 + ファクトリで構造強制) の延長
5. **Repository protocol 経由の DI**: Domain は protocol だけ知り、infrastructure 詳細 (SQLAlchemy session) を持たない (`feedback_session_factory_di.md` と整合)
6. **再利用性**: normal pipeline / maintenance / cron すべてが同じ `try_advance_from` を呼ぶ
7. **Test 容易性**: Repository protocol を mock すれば Domain test だけで rule を精緻に検証できる

### 3.3 Repository に必要な追加 method

`try_advance_from` は「next Stage が未着手か」を確認したいだけで Entity 全体は不要。Repository に **cheap な exists 判定**を追加する:

```python
class AnalysisRepositoryProtocol(Protocol):
    async def exists_for_extraction(self, extraction_id: int) -> bool: ...
```

実装は `select(1) WHERE ... LIMIT 1` 相当で count や Entity 構築は不要。

### 3.4 入口 task (cron / maintenance) の例外

通常 pipeline では「上流 Stage の Task が下流 Stage の Ready を構築」するが、pipeline の入口 (cron 起動 / maintenance task) には上流 Stage が存在しない。これらの task は **自身が gatekeeper を兼ねる**:

| Task 種別 | gatekeeper の所在 |
|---|---|
| pipeline 内部 (B→C→D→E→F) | 上流 Stage の Task が下流 Ready を構築 (Pattern A') |
| 入口 (cron 起動: `discover_articles`, `generate_weekly_snapshot`) | Task 自身が条件を集めて Ready 構築 |
| maintenance (`reclassify_all` 等) | Task 自身が既存 Entity から Ready 構築 |

入口 task でも `Ready.try_advance_from(...)` を呼ぶ形は同じで、Domain の rule 所有は崩さない。

## 4. 楽観的ロック前提の Pipeline 設計と失敗モード

### 4.1 設計思想: Redis Stream broker の at-least-once と楽観的ロックの整合

Vector の broker は Redis Stream (`taskiq-redis` の `RedisStreamBroker`)。Redis Stream の契約は **at-least-once 配信** — メッセージは少なくとも 1 回配信されるが、worker が ACK 前に死ぬと別 worker に再配信される (= **重複配信は仕様**)。

この契約と整合する設計は **消費側 (Worker / Service / Repository) が冪等性を担保すること**。具体的な選択肢として:

| | 思想 | broker at-least-once との相性 |
|---|---|---|
| 悲観的ロック (advisory lock / `SELECT FOR UPDATE` / status カラム予約) | 衝突を未然に防ぐ | **悪い**。lock 取得 worker が ACK 前に死亡すると lock 解放のための別機構が必要 |
| **楽観的ロック** (UNIQUE / `ON CONFLICT` / `UPDATE WHERE`) | 衝突は稀、起きたら検出 | **良い**。worker が死んでも次 worker が UNIQUE で検出、副作用なし |

→ Vector は **楽観的ロック前提**で設計する。Pattern A' (`Ready.try_advance_from` の `exists_for_*`) は precondition の **snapshot 判定**で完全な race-free にはならない (Ready 構築〜INSERT の間に他 worker が先行しうる)。最終的な race 防御は DB UNIQUE 制約 + 楽観的ロック実装で行う。

### 4.2 例外と戻り値の対称性 — race 敗北は戻り値、真の異常は例外

楽観的ロック敗北は **想定内・業務正常**、データ整合性違反は **想定外・バグ indicator**。前者は戻り値、後者は例外で表現する:

| 事象 | 性質 | 表現 |
|---|---|---|
| 期待した UNIQUE 違反 (race 敗北) | 想定内・業務正常 | **戻り値 `Entity \| None`** |
| 期待外の UNIQUE 違反 (別 index への衝突) | 想定外・バグ | **例外伝播** |
| FK 違反 | データ不整合 | 例外伝播 |
| CHECK 違反 | bug indicator | 例外伝播 |
| NOT NULL 違反 | コード bug | 例外伝播 |

`try: ... except IntegrityError: return None` は **5 種を一括で握りつぶす**ので、特に FK / CHECK 違反のバグ検知力を毀損する。ON CONFLICT で **どの衝突だけ握りつぶすか**を SQL レベルで narrow に明示するのが筋。

### 4.3 各 Stage の楽観的ロック実装 (確定版)

#### 4.3.1 INSERT 系: `ON CONFLICT (column) DO NOTHING RETURNING` の index 明示必須

**`ON CONFLICT DO NOTHING` を index 無しで書くと、テーブル上の全 UNIQUE / EXCLUDE 制約違反を握りつぶす**。これは大抵やりたくない。**必ず `ON CONFLICT (column)` または `ON CONFLICT ON CONSTRAINT uq_xxx` で対象 index を明示**する:

```python
# Good: extraction_id への UNIQUE 違反だけ握りつぶす。他の制約違反は例外として上がる
stmt = (
    insert(AnalysisORM)
    .values(...)
    .on_conflict_do_nothing(index_elements=["extraction_id"])
    .returning(AnalysisORM)
)
result = await session.execute(stmt)
row = result.scalar_one_or_none()
return self._to_domain(row) if row else None  # None = race 敗北

# Bad: 全制約違反を握りつぶす — FK 違反まで silently skip される
.on_conflict_do_nothing()  # ← index 指定なし、避けること
```

#### 4.3.2 UPDATE 系: `UPDATE ... WHERE ... RETURNING ...`

`rowcount==1` 判定よりも RETURNING で実書き値を返す方が表現力が高い + 後段で使える:

```python
stmt = (
    update(ArticleAnalysis)
    .where(
        ArticleAnalysis.id == analysis_id,
        ArticleAnalysis.embedding.is_(None),  # 楽観的ロック条件
    )
    .values(embedding=vector, embedding_model=model_name)
    .returning(ArticleAnalysis.embedding, ArticleAnalysis.embedding_model)
)
result = await session.execute(stmt)
row = result.first()
return Embedding.from_row(row) if row else None  # None = race 敗北 or 行不在
```

#### 4.3.3 `try/except IntegrityError` は最後の手段

ON CONFLICT で表現できない複雑な制約 (deferrable constraint, multi-column partial unique index 等) でのみ正当化される。Vector では **現在いずれの Stage も該当しない**。

#### 4.3.4 Stage 別の確定実装

| Stage | 楽観的ロック実装 | index 指定 | 戻り値 |
|---|---|---|---|
| A ingestion | `INSERT ... ON CONFLICT (original_url) DO NOTHING RETURNING ...` | `original_url` | `DiscoveredArticleEntity \| None` |
| B collection.extraction | `INSERT ... ON CONFLICT (discovered_article_id) DO NOTHING RETURNING ...` | `discovered_article_id` | `Article \| None` |
| C analysis.extraction | `INSERT ... ON CONFLICT (article_id) DO NOTHING RETURNING ...` | `article_id` | `Extraction \| None` |
| D classification (Analysis) | `INSERT ... ON CONFLICT (extraction_id) DO NOTHING RETURNING ...` | `extraction_id` | `Analysis \| None` |
| D classification (Rejection) | 同上 | `extraction_id` | `Rejection \| None` |
| E embedding | `UPDATE ... WHERE id=X AND embedding IS NULL RETURNING ...` | (UPDATE 系、index 不要) | `Embedding \| None` |
| F digest | `INSERT ... ON CONFLICT (week_start) DO NOTHING RETURNING ...` | `week_start` | `WeeklyTrendsSnapshot \| None` |

A, B, F は既に ON CONFLICT 構造 — index 指定の有無のみ確認 (PR 内で明示する形に揃える)。C, D, E は Phase PR で実装する。

### 4.4 `rowcount=0` の曖昧性 — Optional[T] で十分

UPDATE 系の `rowcount=0` には 2 つの意味が混在する:

| `rowcount=0` の解釈 | 業務的扱い |
|---|---|
| 該当 id の行が存在しない | 上流 Task のバグ / データ不整合 = **真の異常** |
| 行はあるが他 worker が先行 (例: embedding 既設) | race 敗北 = **業務正常** |

選択肢:

| 案 | 戻り値 | 区別 | 採用 |
|---|---|---|---|
| **(A)** | `Embedding \| None` | 区別しない (Service の handle が同じなら不要) | **採用** |
| (B) | 別途 `select` で行存在を確認、不在なら `NotFoundError` raise | 区別する | 必要が生じたときに B に進化 |

Pattern A' 後は `ReadyForEmbedding` が型で「Analysis 存在 + embedding 未生成」を保証して渡されるため、行不在は **Pattern A' 違反 = 上流バグ**。Phase 2 では (A) で開始、`feedback_verify_before_fallback.md` 思想で必要が生じたら (B) に進化。

### 4.5 失敗モード分類

#### Failure mode 1: 「準備未了」 (expected, **NOT exception**)
- extraction 未生成 / 既に分類済み / 既に却下済み (DB の現状から見た precondition 未充足)
- → `Ready.try_advance_from` が `None` を返す
- → 上流 Task が enqueue しない (= 自然に止まる、業務正常状態)

#### Failure mode 2: データ corruption (unexpected, **exception**)
- DB row 存在するが `translated_title` 空、`id` 負など
- → Ready 型の `__post_init__` が `ValueError` throw
- → Task は **dead letter + alert** (retry しない、data が変わらないと再失敗)
- 隠さず即時死ぬ (`feedback_failure_visibility.md` と整合)

#### Failure mode 3: 実行時 failure (precondition 議論外)
- AI provider error、DB の一時的接続障害など
- → 既存の `ProviderError` / `RetryableError` 階層
- Service は throw、Task が retry

#### Failure mode 4: 楽観的ロック敗北 (broker 重複配信 / 並行 worker)
- Ready 構築〜INSERT の間に他 worker が先行 (Ready は snapshot のため execute 時の race は ready で守れない、§4.1)
- → ON CONFLICT (column) DO NOTHING / UPDATE RETURNING で **Repository が戻り値 None** で表現
- → Service が既存 fetch + 通常 Outcome (§4.6)
- 「期待した衝突」と「期待外の衝突」を SQL レベルで区別 (§4.2 / §4.3.1)

### 4.6 Service / Task の handle

Repository が戻り値 `Entity | None` で楽観的ロック敗北を表現する以上、Service は **SQL メカニズム (rowcount, IntegrityError, ON CONFLICT) を一切知らない**。業務的型のみ見る:

```python
# Service: rowcount や IntegrityError は登場しない。Optional[Entity] だけ
async def execute(self, ready: ReadyForClassification, classifier):
    response = await classifier.classify(...)
    match response:
        case Classified():
            draft = AnalysisDraft.from_classified(response, ready)
            saved = await self._analysis_repo.save(draft)
            if saved is None:
                # 楽観的ロック敗北 → 勝者の永続化結果を読み戻して通常 Outcome
                logger.info("classification_concurrent_write", extraction_id=ready.extraction_id)
                saved = await self._analysis_repo.find_by_extraction_id(ready.extraction_id)
            return ClassifiedOutcome(analysis=saved)
        case OutOfScope():
            ...
```

- **Outcome の variant は変えない** (§2 の縮退方針 `ClassifiedOutcome | RejectedOutcome` を維持、`AlreadyXxxOutcome` 復活させない)
- log で `concurrent_write` を残し可視化 (`feedback_failure_visibility.md` と整合)
- **コスト**: race 敗北側は AI 呼び出し + find_by の 1 SELECT を無駄にする (Embedding でも同等のコストを既に払っている、許容)
- Task は通常 Outcome を受けるので re-enqueue しない

## 5. Pipeline 全 Stage の operation 列挙 (実コード精査済 — 2026-04-28)

精査対象: `backend/app/{collection,analysis,digest}/**/service.py`, `tasks.py`, `domain/`, `repository.py`。

| Stage | Operation | Precondition (rule) | Ready 型のフィールド (operation 必要分のみ) | 既存 UNIQUE / race 構造 |
|---|---|---|---|---|
| **A** ingestion | active NewsSource から記事候補を fetch + 永続化 | NewsSource active + (fetcher の `DAILY_REQUEST_LIMIT` がある場合のみ) Redis quota 残あり | `ReadyForIngestion`: source_id, daily_limit (`int \| None`)。quota 残は run-time 状態のため Ready に含めない | `discovered_articles.original_url` UNIQUE + `INSERT ... ON CONFLICT DO NOTHING RETURNING` |
| **B** collection.extraction | DiscoveredArticle の本文を fetch (HTML パース + 品質ゲート) | Article 未生成 | `ReadyForContentFetch`: discovered_article_id, original_url (`SafeUrl`)。fetcher 設定は registry 経由で Service が解決 | `articles.discovered_article_id` UNIQUE + ON CONFLICT DO NOTHING RETURNING。**race 敗北側は fetch コストを無駄にする** (Ready 型で防げない、§9 で扱う) |
| **C** analysis.extraction | Article 本文を AI で構造化 (translated_title / summary / entities) | Extraction 未生成 + Article が存在 | `ReadyForExtraction`: article_id, original_title, original_content (≤8000 char、超過は Gemini 側で truncate) | `extractions.article_id` UNIQUE。exists method **未実装** (`find_by_article_id` で代用、Ready 型化時に `exists_for_article` 追加要) |
| **D** classification | Extraction を Classified or Rejected に分類 | Extraction 存在 + Analysis 未生成 + Rejection 未生成 | `ReadyForClassification`: extraction_id, translated_title, summary | `analyses.extraction_id` UNIQUE + `rejections.extraction_id` UNIQUE (1 extraction に Analysis xor Rejection)。exists method 未実装 |
| **E** embedding | Analysis に embedding を生成 (`ArticleAnalysis.embedding` カラムを UPDATE) | Analysis 存在 + `embedding IS NULL` | `ReadyForEmbedding`: analysis_id, text_for_embedding (`translated_title + "\n" + summary`)、model_name | **embedding は別テーブルではなく `article_analyses.embedding` カラム**。`UPDATE ... WHERE embedding IS NULL` の rowcount==1 判定で race を構造化済 + DB CHECK 制約 `ck_article_analyses_embedding_consistency` で両カラム整合 |
| **F** digest | 該当週の trend snapshot を生成 (1 週 = 1 行 JSONB) | 該当週の snapshot 未生成 (force=True なら override) | `ReadyForDigest`: week_start (JST 月曜起点)、force | `weekly_trends_snapshots.week_start` PK (UNIQUE) + `INSERT ... ON CONFLICT DO NOTHING` |

### 5.1 Stage 間の入力受け渡し (現状)

すべての Task は `int` (article_id / source_id / discovered_article_id) を受け渡し、Service 側で session を開いて Repository から Entity を再取得する形。**Ready 型化後は Task → Task 間で Ready 型自体を `kiq` する**ため、Stage 境界での DB 再 fetch が削減される。

### 5.2 入口 task (cron) は 2 つだけ

- `dispatch_sources` (Stage A 起動、`broker_metadata`、cron `0 * * * *`)
- `generate_weekly_snapshot` (Stage F 起動、`broker_digest`、cron `5 15 * * 0` UTC = JST 月曜 00:05)

両方とも **§3.4 の入口 task pattern**: Task 自身が `Ready.try_advance_from(...)` を呼ぶ形に揃える。

## 6. Domain factory のテンプレート

Ready 型は **Pydantic `BaseModel(frozen=True)` で実装**する。`@dataclass(frozen=True,
slots=True)` ではなく BaseModel を選ぶ理由は taskiq の formatter 制約 (Phase 1 で
判明、Issue #441 / #558。詳細 memory `feedback_taskiq_basemodel_required.md`)。
JSONSerializer のまま動き、追加依存ゼロ。

```python
class ReadyForX(BaseModel):
    """X operation を実行可能な状態。

    フィールドは operation に必要な値だけ。Aggregate Entity 全体は持たない。
    """

    model_config = ConfigDict(frozen=True)

    upstream_field: str           # upstream Entity からのコピー (再 check 不要)
    derived_field: str = Field(min_length=1)  # 派生フィールドは構築時 validate
    
    @classmethod
    async def try_advance_from(
        cls,
        upstream: UpstreamEntity,
        # next Stage の precondition を判定するための Repository protocol
        downstream_repo_a: DownstreamRepoAProtocol,
        downstream_repo_b: DownstreamRepoBProtocol,
    ) -> ReadyForX | None:
        """upstream Stage 完了から X Stage へ advance できるかを判定。
        
        Precondition (X Stage に進める条件):
        - downstream_repo_a で対応 record が未生成
        - downstream_repo_b で対応 record が未生成
        
        Returns:
            進める場合: ReadyForX (operation 必要分の field のみ)
            進めない場合: None (業務正常状態、例外ではない)
        """
        if await downstream_repo_a.exists_for(upstream.id): return None
        if await downstream_repo_b.exists_for(upstream.id): return None
        return cls(
            upstream_field=upstream.field_x,
            derived_field=f"{upstream.field_a}\n{upstream.field_b}",
        )
```

### 6.1 設計のポイント

1. **classmethod は Domain layer 内**: Domain 層の Ready 型が rule を所有し、Repository を **protocol** で受ける (具体実装は infrastructure 側)
2. **入力は upstream Entity + 下流 Repository protocol**: 入力は Aggregate Entity を受けるが、出力 (Ready 型自身) は operation 必要分の field だけ。**入出力の非対称**
3. **session を Domain に渡さない**: Repository protocol だけ受け、session 管理は呼び出し側 (Task) の責務 (`feedback_session_factory_di.md` と整合)

### 6.2 不変条件 check のスコープ — 派生フィールドのみ

判定軸: フィールドが「**upstream Entity からのコピー** か **try_advance_from で
組み立てた派生値** か」。

| フィールドの出所 | 構築時 check するか | 理由 |
|---|---|---|
| upstream Entity の値を直接コピー | **しない** | upstream の `__post_init__` / Pydantic validator が既に invariant 保証済。再 check は冗長な重複記述 |
| try_advance_from が組み立てた派生値 | **する** | この型でしか守れない不変条件 (組み立て後の non-empty / max length 等)。`Field(min_length=1)` / `model_validator(mode='after')` で表現 |
| 入口 task で外部から受け取る値 (cron 引数等) | **する** | upstream Entity が存在せず、Ready 型が初の構築時 gatekeeper になる |

`feedback_structural_guarantee.md` (DB 制約 + ファクトリで構造保証、ランタイム
if-raise 排除) との関係: Pydantic validator は **構築時 1 回**のみ実行され、構造
保証の一部。「処理ごとに毎回 if-raise」のランタイムチェックではない。

### 6.3 Stage 別の構築時 check 内容 (派生フィールドの有無)

| Stage | フィールド | 派生 / 入口 値 | 構築時 check |
|---|---|---|---|
| D classification | extraction_id, translated_title, summary, article_id (Phase 1 transitional) | すべて upstream copy | **不要** (upstream 側で保証済) |
| E embedding | analysis_id, text_for_embedding, model_name | text_for_embedding は title + summary の派生 | `Field(min_length=1)` で text_for_embedding non-empty |
| C analysis.extraction | article_id, original_title, original_content | すべて Article copy | **不要** |
| F digest | week_start, force | week_start は cron 引数 (入口) | `model_validator` で week_start が月曜 (`weekday() == 0`) を check |
| A ingestion | source_id, daily_limit | daily_limit は fetcher 由来 (入口) | `Field(gt=0)` で source_id > 0 |
| B collection.extraction | discovered_article_id, original_url | すべて DiscoveredArticle copy | **不要** |

### 6.4 test 戦略 (各 Phase PR の checklist step 7 を具体化)

| test 対象 | 必須 / 任意 | 内容 |
|---|---|---|
| `try_advance_from` の precondition 充足 (Ready 返却) | **必須** | Repository mock で「下流未生成」状態を作り、Ready 返却を assert |
| `try_advance_from` の precondition 未充足 (None 返却) | **必須** | 各 `exists_for_*` を順番に True にして None 返却を assert |
| `__post_init__` の ValueError | **派生 / 入口 フィールドがある Stage のみ** | E (text_for_embedding 空), F (week_start が火曜), A (source_id ≤ 0) など |
| Repository integration test | **別 PR スコープ** | 実 DB で `exists_for_*` を検証、Phase PR の checklist には含めない |

## 7. Task の組み立てパターン

### 7.1 pipeline 内部 (上流 Stage の Task が下流 Ready を構築)

```python
@broker_extraction.task(...)
async def extract_content(article_id: int) -> None:
    # Stage C 本来の仕事
    extraction = await _do_extraction(article_id)
    
    # Stage D へ進めるかは Ready 型に問い合わせる (Stage C は Stage D の rule を知らない)
    async with session_factory() as session:
        analysis_repo = AnalysisRepository(session)
        rejection_repo = RejectionRepository(session)
        ready = await ReadyForClassification.try_advance_from(
            extraction,
            analysis_repo=analysis_repo,
            rejection_repo=rejection_repo,
        )
    
    if ready is not None:
        await classify_content.kiq(ready)   # Ready 型自体を enqueue
```

```python
@broker_classification.task(...)
async def classify_content(ready: ReadyForClassification) -> None:
    # 受け取った時点で precondition は型で保証済み (Stage C の Task が gatekeeper した結果)
    # fetch も None check も不要
    svc = ClassificationService(session_factory)
    result = await svc.execute(ready, classifier)
    
    # 次 Stage の Ready 構築は Service の戻り値 type で分岐
    if isinstance(result, ClassifiedOutcome):
        async with session_factory() as session:
            embedding_repo = EmbeddingRepository(session)
            ready_emb = await ReadyForEmbedding.try_advance_from(
                result.analysis,
                embedding_repo=embedding_repo,
            )
        if ready_emb is not None:
            await generate_embedding.kiq(ready_emb)
```

### 7.2 入口 task (cron / maintenance)

```python
# maintenance task の例 (上流 Stage が存在しない、自身が gatekeeper)
async def reclassify_all() -> None:
    async with session_factory() as session:
        extraction_repo = ExtractionRepository(session)
        analysis_repo = AnalysisRepository(session)
        rejection_repo = RejectionRepository(session)
        extractions = await extraction_repo.find_pending()
        for extraction in extractions:
            ready = await ReadyForClassification.try_advance_from(
                extraction,
                analysis_repo=analysis_repo,
                rejection_repo=rejection_repo,
            )
            if ready is not None:
                await classify_content.kiq(ready)
```

入口 task でも同じ `try_advance_from` を呼ぶ → rule の所在は変わらず Domain。

### 7.3 taskiq parameter として Ready 型を渡す

Ready 型は `BaseModel(frozen=True)` で実装する (Phase 1 で確定)。理由は taskiq の
formatter が Pydantic ベースで、kiq 引数に未知型 (素の `@dataclass`) を渡すと
serializer 到達前に `PydanticSerializationError` で死ぬため (Issue #441)。
BaseModel は Issue #558 で公式サポート、JSONSerializer のまま追加依存ゼロで動く。

詳細は memory `feedback_taskiq_basemodel_required.md`。将来 throughput / size が
問題になったら msgpack に差し替えるのは 1 行 (formatter は変えなくて良い、後回し
戦略可)。

## 8. PersistedXxxId / Entity.from_draft 削除を各 Phase に内包する

Ready 型化リファクタは「Service の Entity 組み立て責務」を消すため、PersistedXxxId と Entity.from_draft 削除を**各 Phase の同一 PR に同梱する** (cleanup を別 PR に分けない)。旧 spec `specs/persisted-id-removal-plan.md` は既に削除済みで、本 spec が後継。

### 8.1 Repository.save の新しい戻り値

`Repository.save(draft) -> Entity | None` (楽観的ロック前提、§4.3 と整合)。

- **成功時**: Entity を返す
- **race 敗北時**: `None` を返す (期待した UNIQUE 違反のみ、§4.3.1 で index 明示)

Service は Entity を受け取ってそのまま Outcome に詰めるだけで、Entity の組み立てを行わない。`Entity.from_draft` は Repository 内部の `_to_domain` / 直接構築に置き換えて完全削除。

実装は **`INSERT ... ON CONFLICT (column) DO NOTHING RETURNING ...`** (§4.3.1)。`try/except IntegrityError` は最後の手段で、Vector の現 Stage では使わない。

> **Embedding 系の例外**: `EmbeddingRepository.save` は INSERT ではなく UPDATE pattern。戻り値は同じく `Embedding | None` だが、実装は `UPDATE ... WHERE ... RETURNING ...` (§4.3.2)。`bool` から `Embedding | None` に変更する (Phase 2 PR で実装変更)。

### 8.2 各 Phase で削除する対象

| Phase | Stage | 削除する型・factory |
|---|---|---|
| 1 | D classification | `PersistedAnalysisId`, `PersistedRejectionId`, `Analysis.from_draft`, `Rejection.from_draft` |
| 2 | E embedding | `Embedding.from_draft` (UPDATE WHERE pattern なので `PersistedEmbeddingId` は元々ない) |
| 3 | C analysis.extraction | `Extraction.from_draft` (現状未使用なら確認、`PersistedExtractionId` も同様) |
| 4 | F digest | (該当なし、永続化単位が JSONB 1 行) |
| 5 | A ingestion | `DiscoveredArticleEntity.from_draft` |
| 6 | B collection.extraction | `PersistedArticleId`, `Article.from_draft` |

### 8.3 Phase PR の standard checklist

各 Phase PR は以下をすべて含む (atomic):

1. `ReadyForX` 型 (`BaseModel(frozen=True)`、§7.3) + `try_advance_from` の追加 (Domain layer)
2. Repository に `exists_for_*` method 追加 (`exists_for_extraction`, `exists_for_article` 等、§5.2 リスト)
3. Repository.save を **`Entity \| None` 戻り値 + `ON CONFLICT (column) DO NOTHING RETURNING`** に変更 (§4.3.1, §8.1)。INSERT 系は index 明示必須。E のみ `UPDATE ... RETURNING` (§4.3.2)
4. Service.execute の signature 変更 (`Ready` 型を受け取る、precondition 分岐削除)。race 敗北時は `find_by_xxx` で読み戻し + 通常 Outcome (§4.6)
5. Task の chain 経路変更 (上流 Task が `Ready.try_advance_from` → `kiq`)。maintenance task / 入口 task も同 pattern (§3.4 / §7.2)
6. 該当 Stage の `PersistedXxxId` / `Entity.from_draft` 削除
7. Domain factory の unit test (Repository protocol mock) 追加 (§6.4)

## 9. 横展開順序

提案: precondition rule が単純な D から着手し、Pipeline 内部 Pattern A' を確立後に入口 task に展開。

1. **D: classification** (パイロット。precondition 判定が現状最も明確 + 議論済み)
2. **E: embedding** (UPDATE WHERE pattern が既に rowcount で構造化済み、Stage 内部の race 構造を維持しつつ Ready 型化)
3. **C: analysis.extraction** (D と類似、shape を transfer しやすい。本文 8000 char truncate は extractor 内部の話で Ready 型に持たない)
4. **F: digest** (入口 task pattern の確立。Stage A より構造が単純なので先に実施)
5. **A: ingestion** (入口 task。fetcher 種別差 + quota 状態の責務切り分け要、整合性確認の最後)
6. **B: collection.extraction** (race-loss 設計が最も複雑。fetch を無駄にしない最適化は別 PR スコープ)

各 Stage 1 PR、合計 6 PR の見込み。

**Stage B の race-loss について**: Ready 型化は precondition の構造化のみ扱い、race 敗北時の fetch コスト最適化 (advisory lock / status カラムによる予約等) は本リファクタの範囲外。spec §4 の Failure mode 4 に従い DB UNIQUE + ON CONFLICT で意味的整合は保たれる。

## 10. spec の TODO

- [x] 各 Stage の precondition rule を実コードから精査して埋める (§5、2026-04-28 完了)
- [x] Ready 型のフィールドを「operation に必要な値だけ」基準で確定する (§5、2026-04-28 完了)
- [x] Stage 間の chain 経路 — Pattern A' (上流 Task が下流 Ready を構築 + 入口 task は自身が gatekeeper)
- [x] race condition の方針 — DB UNIQUE 制約 + IntegrityError (§4 Failure mode 4)
- [x] テスト戦略 (§6.4 で確定: try_advance_from の precondition 充足/未充足は必須、`__post_init__` の ValueError は派生/入口フィールドある Stage のみ、Repository integration test は別 PR スコープ)
- [x] 旧 spec `persisted-id-removal-plan.md` の扱い — ファイルは既に削除済、PersistedXxxId / Entity.from_draft 削除を各 Phase に内包する方針で本 spec §8 に統合 (2026-04-28)

## 11. 将来課題: chain → scheduler 移行 (本リファクタの範囲外、2026-04-28 記録)

本リファクタは **chain 方式** (上流 Task が下流 Ready を構築 + `kiq`、Pattern A') を維持する。一方、**scheduler 方式** (各 Task は自分の永続化まで責任、下流投入は専用 cron が DB polling) という選択肢があり、Pattern A' の coupling は型レベルに薄めただけで完全には消えない。これを将来課題として記録する。

### 11.1 現状 chain 維持の根拠

1. レイテンシは Vector の中核 UX (新記事の発見〜分類完了の時間)
2. Pattern A' で coupling は型 (Ready 型 + Repository protocol) に閉じ込め済 — Service / classifier 中身は知らない
3. scheduler 化は Stage 5 個分の polling 機構 + 重複投入防止の実装で重く、本リファクタ 6 PR を遥かに超える工数
4. 既存 Hybrid (chain + `backfill_*` maintenance) で chain 切断耐性は確保されている

### 11.2 scheduler 移行が議論再燃する shape

- Stage 数が増加 (新たな AI 分析 Stage の追加で coupling 管理コストが上がる)
- 別 pipeline (例: 第二言語版) との統合
- レイテンシ要求が緩和 (週次配信のみで十分になる等)
- DB polling コストが許容内 (記事数増でも index で抑えられる)

### 11.3 移行時に再利用できる / 捨てる部分

| Pattern A' の要素 | scheduler 移行時 |
|---|---|
| Ready 型 + `try_advance_from` (Domain) | **そのまま再利用**。scheduler が呼ぶ形に切り替えるだけ |
| `exists_for_*` Repository protocol | **そのまま再利用**。scheduler の polling SQL と並立 |
| 上流 Task が `Ready.try_advance_from` → `kiq` する経路 | **削除**。各 Task は自分の永続化で完結 |
| §3.4 の入口 task 特例 | **消滅**。全 Stage が同 pattern (scheduler 起動) に揃う |

つまり Pattern A' の **Domain 層投資 (Ready 型 + try_advance_from + Repository protocol) は scheduler 移行でも生き残る**。chain と scheduler の差は「呼び出し主体」だけで、Domain 設計は共通。本リファクタは将来移行の妨げにならない。
