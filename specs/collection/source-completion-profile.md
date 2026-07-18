> **Vocabulary note (2026-05-25)**: 本文に残るstage1旧語彙 (`source_fetch` / `article_collection` /
> `SourceFetch*` / `ingest_source`) は **acquisition** に統一済。語彙の正本は
> [`stage1-acquisition-vocabulary-unification.md`](./stage1-acquisition-vocabulary-unification.md)。
>
> 主要語彙 (旧 → 新): `source_fetch` → `acquisition` (stage token / `kind`) /
> `article_collection` → `article_acquisition` (dir) / `SourceFetchError` → `SourceAcquisitionError` /
> `SourceFetchFailureHandler` → `SourceAcquisitionFailureHandler` /
> `SourceFetchAuditRepository` → `SourceAcquisitionAuditRepository` /
> `SourceFetchPayload` → `AcquisitionPayload` / `ingest_source` → `acquire_source` (task) /
> `IngestSourceArg` → `AcquireSourceArg`。`fetch` I/O 基層 (ExternalFetchError / FetchedArticle /
> FetchLog 等) は据え置き。本specの補完ポリシー契約は有効で、旧語彙だけを現行仕様として読まないこと。

# Source 集約と Source Completion Profile — 補完ポリシーの per-source 化 実装仕様

作成日: 2026-05-17
Status: **Implemented: P1 + P2(B+C) + P2-D + P3**（本 spec は実装確定形へ整合済 = 記録 SSoT）。Pattern (R/H) は P3 で `SourceCompletionProfile.precludes_stage1_ready()` への per-field 導出へ解消（旧 `force_html_title` title 単独 hardcode を撤廃、45 ソース R/H 出力 byte 不変・`passport_builder.py` は P3 で diff 発生）。
ブランチ: P1 は `main` に merge 済（6 コミット）。P2(B+C) は `refactor/source-aggregate-p2`（C1-C3、byte 不変、merge 済 #545）。P2-D（Adapter 概念除去）は `refactor/source-collapse-p2d`（C1 加法 / C2 原子 flip / C3 整理 の 3 コミット、byte 不変）。
関連 spec: [`stage1-acquisition-vocabulary-unification.md`](./stage1-acquisition-vocabulary-unification.md) /
[`typed-pipeline-preconditions.md`](../pipeline/typed-pipeline-preconditions.md)

> **実装確定後の精緻化（再議論防止 — 詳細は §9 決定ログ）**
> 設計段階の語彙から実装で以下を確定した。本文は確定形で記述する:
> `ObservedArticleSnapshot`→**`ObservedArticle`**（`Snapshot` は実装語彙のため除去・`domain/` 配置）/ `FieldCompletionPolicy` は DU でなく **`StrEnum`** / `IncompleteArticle` は改名でなく**削除**・`CompletionPendingArticle` は**作らない**（束ねは use-case DTO `ReadyForArticleCompletion`）/ 昇格は free function **`complete_with_html`**（`promotion.py`）/ **P1 では Adapter→Source rename をしない**（per-source データは既存 `SourceAdapter` の ClassVar に追加）。
> **P2(B+C) で確定**: 「`SourceAdapter`→`ArticleSource` rename」は **新規集約 `ArticleSource` の追加**として再解釈（Protocol 改名ではない）。`SourceAdapter` Protocol は **machinery 契約として保持**し `collect()` のみへ slim（identity/policy 属性を除去）。`XxxAdapter` は rename せず **de-ClassVar**（取得 machinery として残置、config を `__init__` 注入）。registry は `dict[SourceName, ArticleSource]` インスタンス + 遅延 `adapter_factory`（無 instantiation 契約を class-ref でなく**設計**で担保）。(R/H) Profile 導出は挙動変化を含むため **P3** へ分離。

対象（P1 実装で触れたファイル）:

- `backend/app/collection/domain/observed_article.py`（**新規**。`StagedArticleAttributes` を置換する取得事実 VO）
- `backend/app/collection/domain/source_completion_profile.py`（**新規**。Profile + `FieldCompletionPolicy` StrEnum）
- `backend/app/collection/article_completion/promotion.py`（**新規**。free function `complete_with_html`）
- `backend/app/collection/sources/profile_resolver.py`（**新規**。`CompletionProfileResolver` Protocol + Registry 具象）
- `backend/app/collection/domain/incomplete_article.py` / `backend/app/collection/persistence/staged_attributes.py`（**削除**）
- `backend/app/collection/domain/analyzable_article.py`（不変・出口契約として維持）
- `backend/app/collection/article_completion/repository.py`（hydrate = ACL の中核。identity 注入 + profile 解決）
- `backend/app/collection/article_completion/service.py` / `completer.py` / `ready.py`
- `backend/app/collection/source_fetch/pending_enqueue.py` / `service.py`（Stage 1 enqueue）
- `backend/app/collection/fetchers/strategy.py` / `article_fetcher.py` / `protocol.py`
- `backend/app/collection/fetchers/tools/fetched_article.py`（`SourceAdapter` Protocol に 2 ClassVar 契約追加）
- `backend/app/collection/fetchers/tools/passport_builder.py`（Pattern R/H 分岐は P1 温存）
- 共有基底 4 + standalone adapter 群（`observed_origin` / `completion_profile` ClassVar 追加。`anthropic.py` / `ornl.py` は特例 override）
- `backend/app/collection/article_completion/dispatch.py`（Stage 2 cron poller）
- `backend/app/models/pending_html_article.py` / `backend/app/models/news_source.py`（**スキーマ不変** — JSONB 契約のみ変化）

---

## 1. 目的

「外部取得で部分的にしか満たせない記事を、二段補完で `AnalyzableArticle`（全フィールド必須）へ昇格させる」ドメインモデルを、**ベストプラクティスに基づき**再設計する。現状実装に合わせるのではなく「この問題はどう解くべきか」から逆算する。

### 1.1 解くべき問題

現状、記事の「不足表現」が **3 つの異なる語彙**に散っている:

| フィールド | 現状の「不足」表現 | 問題 |
|---|---|---|
| `body` | フィールド自体が不在（常に要補完と暗黙固定） | 語彙 A |
| `published_at` | `published_at_hint: PublishedAt \| None`（nullable） | 語彙 B |
| `title` | `prefer_html_title: bool`（しかも title は required） | 語彙 C |

ソースが増えるたびに新フラグ＋ `complete_with_html` 内の分岐が線形に腐る。多源ニュースアグリゲータでパターン増加はほぼ確実なので、これは投機ではなく既に発生している負債。

### 1.2 核心の気付き — 議論の粒度が誤っていた

「nullable mirror 型」案も「per-field discriminated union を記事に載せる」案も、**どちらも補完状態を記事インスタンスに載せていた**。リサーチ §3（後述・出典あり）が決定的に否定する:

> どのフィールドを fetch する必要があるか／どちらを正本とするかは、記事が下す判断ではなく **ソースの出自(provenance)で構造的に決まる**。RSS は構造的に body を欠き、sitemap 系は title を欠く。これは**ソース種別の能力(capability)**であって記事ごとの自由状態ではない。それを全記事インスタンスに booleans/None で複写するのは Minsky の「実態と食い違える per-instance optional」アンチパターン。

### 1.3 確定方針（1 行）

**補完ポリシーは per-source、取得事実は per-article。取れた事実は全部保存し、方針(profile)が利用可否を決める。** ポリシーは `SourceCompletionProfile` に集約する。**P1 では** per-source 知識（補完方針 `completion_profile` / 取得出自 `observed_origin`）を既存 `SourceAdapter`/`XxxAdapter` クラスの `ClassVar` として宣言する（Adapter は「ソースごとの事情を per-source クラスに定義」が既に成立しているため、属性を 2 つ足すだけ）。`Source` 集約への昇格と Adapter→Source rename は **P2**（機能変更と混ぜない）。pending JSONB には「補完状態」ではなく「取得済み事実 = `ObservedArticle`」だけを保存する。policy の場合分けは記事ではなく **Profile 側の `FieldCompletionPolicy`（`StrEnum`）** で表す（payload ゼロの 3 variant に DU は ceremony 過剰 — §4.2/§9）。

---

## 2. リサーチ根拠（権威ある一次情報源）

| 論点 | 結論 | 出典 |
|---|---|---|
| §1 未完成→完成エンティティ | 出力 `AnalyzableArticle` を「不完全を表現不能」な別型にし、二段目 fetch を validator でなく **parser**（smart constructor）として扱う。観測事実型 / 完成型の二型分離は正しい（実装: `ObservedArticle`→`AnalyzableArticle`。旧 `IncompleteArticle` は削除しこの後継へ） | Alexis King "Parse, don't validate" / Yaron Minsky "Effective ML"（state ごと別型）/ Scott Wlaschin "Designing with types: representing states" |
| §2 per-field DU vs flat | DU は「状態が固有データを持つ／下流挙動を変える」かつ flat だと illegal combination が生じる場合にのみ正当。`present-but-prefer-source` 第3状態が決定要因。ただしこれ自体が source-profile 属性 | Wlaschin "making illegal states unrepresentable"（"when NOT to use" 含む）/ Minsky |
| §3（最重要）instance state vs source capability | 「どのフィールドが要 fetch／優先か」は **per-source の capability/profile**。記事インスタンスは fetch の **残余 binary outcome** のみ持つ。状態をインスタンスに散らすのは Minsky アンチパターン | Wlaschin "representing states"（states lack domain importance）/ "making illegal states unrepresentable"（型がルールを自己文書化）|
| §4 wire DTO + ACL | 永続化 DTO + 翻訳層は「永続形と domain 形に構造 mismatch がある場合に正当」。本件はその mismatch があるので妥当（gratuitous でない） | Martin Fowler "LocalDTO" / Vaughn Vernon *IDDD* Anti-Corruption Layer |

出典 URL は §10 に集約。

---

## 3. 現状コードの確認（grounded）

> 本節は **P1 着手前のベースライン**（リファクタの動機付け = §1.1 の 3 語彙問題の根拠）。`prefer_html_title` / `IncompleteArticle` / `StagedArticleAttributes` 等の記述は「リファクタ前にこうだった」の記録であり、確定形は §4 以降。

### 3.1 Fetcher / Source 配線

- Fetcher 群: `backend/app/collection/fetchers/`（45+ ソース）。基底は `Fetcher` Protocol（`protocol.py:36-61`、structural subtyping、`fetch(source_id) -> AsyncIterator[AnalyzableArticle | IncompleteArticle]`）。
- 登録: `strategy.py` の `FETCHERS: dict[str, Callable[[], Fetcher]]`。キーは `news_sources.name`（`SourceName` StrEnum）、値は `lambda: ArticleFetcher(XxxAdapter())`（毎回 new・無状態）。
- `ArticleFetcher`（`article_fetcher.py:25-45`）は adapter の NAME/ENDPOINT_URL を instance attr に格上げ。
- **Stage 2 は fetcher を生成しない**（`dispatch.py:34`〜は HTML fetch のみ）。よって「fetcher は Stage 1 専用」。Stage 1/2 双方が必要とするのは **Source の policy 面**。

### 3.2 Source レジストリ

- `news_sources` ORM（`backend/app/models/news_source.py:47-63`）: `id` / `name: SourceName`(UNIQUE) / `source_type: SourceType`("rss"/"api"/"html") / `site_url` / `endpoint_url`(UNIQUE) / `is_active` / `attribution_label`。
- `source_type` は **audit-only**。runtime の fetcher 選択は `name` lookup のみ。`source_id → ソース種別 → profile` の lookup 経路は**存在しない**。
- ソース固有設定は Adapter ClassVar に hardcode（ENDPOINT_URL / MAX_ENTRIES / EXCLUDED_PATHS 等）し、ソース単位の設定を対応するmoduleに閉じ込める。

### 3.3 prefer_html_title の発生源（特例は 2 ソースのみ）

| Fetcher | file:line | 理由 |
|---|---|---|
| AnthropicAdapter | `fetchers/anthropic.py:121` | sitemap に title なし → URL slug placeholder |
| ORNLAdapter | `fetchers/ornl.py:129` | listing に title なし → URL slug placeholder |

伝播: `FetchedArticle.prefer_html_title` → `passport_builder.try_build_passport()`（`passport_builder.py:84` の `not prefer_html_title` が Ready/Pattern R 経路を止める条件）→ `IncompleteArticle`（`incomplete_article.py:41`）→ `StagedArticleAttributes`（`pending_enqueue.py:64`）→ Stage 2 `complete_with_html`（`incomplete_article.py:69-71`）。

### 3.4 PendingHtmlArticleORM

`backend/app/models/pending_html_article.py:36-119`。主カラム: `id`(PK) / `url`(UNIQUE) / `source_id`(FK news_sources, RESTRICT) / `status`(CHECK ∈ open/running/closed) / `staged_attributes`(JSONB) / `ready_at` / `leased_until` / `attempt_count`(CHECK≥0) / timestamps。state consistency CHECK あり。
`staged_attributes` = `StagedArticleAttributes`(frozen Pydantic: `title:str` / `published_at_hint:PublishedAt|None` / `prefer_html_title:bool`).model_dump(mode="json")。読出は `model_validate`。

### 3.5 Stage 2 フロー

`dispatch_html_fetch_jobs`（`dispatch.py:34-61`、cron 1 分）→ `claim_ready_batch`（open & ready_at≤now を FOR UPDATE SKIP LOCKED で claim、open→running）→ `extract_html_body.kiq(pending_id)`（`tasks.py:207-260`）→ `ReadyForArticleCompletion.try_advance_from`（`ready.py:62-88`）→ `repo.try_load_for_completion`（`repository.py:49-91`、`staged_attributes` JSONB → `IncompleteArticle` 再構築 = **ACL の現実装点**、`repository.py:84-90`）→ `ArticleCompletionService.execute`（`service.py:97-153`）→ `ArticleHtmlCompleter.complete` → `IncompleteArticle.complete_with_html`（`incomplete_article.py:43-86`）→ 成功時 `persist_completed`（`repository.py:203-219`、`_delete_claimed` 後 `AnalyzableArticleRepository.save`）。

### 3.6 AnalyzableArticle 永続化先

`analyzable_articles` テーブル（`backend/app/models/analyzable_article_record.py`）。
`AnalyzableArticleRepository.save`（`persistence/analyzable_article_repository.py`、ON CONFLICT DO NOTHING / RETURNING id）。Pattern R/H 共通。`AnalyzableArticle`（`analyzable_article.py`）は **出口契約として不変**。

---

## 4. 目標設計

### 4.1 per-source 知識の集約（P1: Adapter ClassVar / P2: Source 集約）

「ニュースソース」の知識が散在している（Adapter / `prefer_html_title` / `news_sources` 行 / `strategy.py`）。最終形は **1 つの `Source` 集約**が (a) どう fetch するか=Adapter、(b) どう完成させるか=`SourceCompletionProfile` を所有し、状態移管の条件を型で集約する形とする。

**P1（実装済）= 機能のみ。Adapter→Source rename はしない。** recon で共有基底 4 + 継承 13 の階層が判明し、rename 単独だと is-a 違反、正すには継承→委譲が不可分（B+C は機能変更と混ぜず P2）。よって P1 は per-source データの置き場を **既存 `SourceAdapter`/`XxxAdapter` クラス**（`NAME`/`ENDPOINT_URL`/`FEEDS`/`_build_body` 等で per-source 事情を既にクラスに定義済）とし、そこへ 2 属性を **ClassVar で追加するだけ**:

```python
# fetchers/tools/fetched_article.py: SourceAdapter Protocol (名前据え置き) に契約追加
class SourceAdapter(Protocol):
    NAME: str
    ENDPOINT_URL: str
    observed_origin: ObservedOrigin              # 追加 (取得出自・audit)
    completion_profile: SourceCompletionProfile  # 追加 (補完方針)
    def collect(self) -> AsyncIterator[FetchedArticle]: ...

# 共有基底 4 個に default ClassVar (継承 13 具象は無改修)
class BaseMultiFeedRssAdapter:
    observed_origin = ObservedOrigin.feed
    completion_profile = DEFAULT_PROFILE

# 特例 standalone のみ override (anthropic=sitemap+HTML_TITLE_PROFILE /
# ornl=listing+HTML_TITLE_PROFILE / hacker_news=api)。他は feed + DEFAULT。

# fetchers/strategy.py: Stage 2 resolver が無 instantiation で読む registry
SOURCES: Final[dict[str, type[SourceAdapter]]] = {A.NAME: A for A in _ADAPTERS}
FETCHERS: Final[dict[str, Callable[[], Fetcher]]] = {
    name: (lambda A=A: ArticleFetcher(A())) for name, A in SOURCES.items()
}
```

P1 では `ArticleFetcher` はコンストラクタ契約不変（`__init__(self, adapter)`）、policy は adapter ClassVar から thread、`SOURCES` は `type[SourceAdapter]`（class 値）で Stage 2 resolver が無 instantiation で `.completion_profile` を読む。

**P2(B+C)（実装済）= 構造のみ・byte 不変**。最終形「1 つの `ArticleSource` 集約が (a) どう fetch=`adapter_factory` 経由 machinery、(b) どう完成=`completion_profile`」へ収束した:

```python
# sources/article_source.py（新規集約。SourceAdapter Protocol の改名ではない）
@dataclass(frozen=True, slots=True)
class ArticleSource:
    name: SourceName
    endpoint_url: str
    observed_origin: ObservedOrigin
    completion_profile: SourceCompletionProfile
    adapter_factory: Callable[[], SourceAdapter]
    def make_adapter(self) -> SourceAdapter: return self.adapter_factory()

# fetchers/tools/fetched_article.py: SourceAdapter Protocol は machinery 契約へ slim
class SourceAdapter(Protocol):                    # 名前据え置き・collect() のみ
    def collect(self) -> AsyncIterator[FetchedArticle]: ...

# 共有基底 4 → 汎用 machinery（継承拡張点 _build_body override → 注入 body_builder）
#   BaseMultiFeedRssAdapter→MultiFeedRssAdapter / BaseDjangoplicityAdapter→
#   DjangoplicityAdapter / BaseFrontiersJournalAdapter→FrontiersJournalAdapter /
#   BaseMDPICrossrefAdapter→MDPICrossrefAdapter
# 継承 12 具象解体（NASA/Cornell は config を module-level 化、ESA×2/
#   Frontiers×4/MDPI×4 thin subclass は削除し identity/ISSN を factory へ移送）
# standalone 33 は machinery 残置・de-ClassVar（endpoint/source_name を __init__ 注入、
#   collect() body は byte 不変。over-generalize しない＝挙動 0 が安全性質）

# fetchers/strategy.py: ArticleSource インスタンスのレジストリ
SOURCES: Final[dict[SourceName, ArticleSource]] = {s.name: s for s in _SOURCES_LIST}
FETCHERS: Final[dict[str, Callable[[], Fetcher]]] = {
    str(s.name): (lambda s=s: ArticleFetcher(s)) for s in _SOURCES_LIST
}

# fetchers/article_fetcher.py: コンストラクタ契約変更（P2 で許容）
class ArticleFetcher:
    def __init__(self, source: ArticleSource) -> None: self._source = source; ...
    async def fetch(self, source_id): adapter = self._source.make_adapter(); ...
```

`adapter_factory` は遅延 callable のため registry 構築（module import）時に `RssParser()`/`CrossrefApiClient()` は構築されない。Stage 2 の profile 解決は `source.completion_profile` 直読みで `make_adapter()` を呼ばない。よって**無 instantiation 契約**（§4.6）は P1=class-ref 保持 / P2=factory 非呼出 で担保された。

**P2-D 確定形（Adapter 概念除去）**: `adapter_factory` / `make_adapter` / `SourceAdapter` Protocol を**全廃**。1 ソース = 1 `XxxSource` クラスが identity / 補完方針を `ClassVar` 宣言し `collect(cls, tools)` を classmethod 実装する → **クラスオブジェクト自体**が `ArticleSource` Protocol（instance 形シグネチャ、`@runtime_checkable`）を構造的に満たす。`SOURCES: dict[SourceName, ArticleSource]` の値は Source クラスそのもの。共有 machinery（NASA/Cornell の `multi_feed_rss` / ESA `djangoplicity_entries` / Frontiers `frontiers_entries` / MDPI `mdpi_items`）は `FetchTools` を引数に取る **free function**、具体 Source は `esa|frontiers|mdpi/sources.py`（`_common.py` は共通処理のみ）。`ArticleFetcher` は `FetchTools`（共通取得道具箱・純 I/O）を Source へ渡すだけの薄い runner。これにより無 instantiation 契約は「profile を読むのに machinery を作る経路が**構造的に不能**」= class-ref 構造保証（P2 の設計担保より強い）へ昇格。`FETCHERS` は `str(name)` キーのため `tasks.py:148` の `FETCHERS[arg.name]` は無改修。

**P3（実装完了・別 PR）= (R/H) のみ**: `passport_builder.py` の Pattern R/H hardcode（`force_html_title = profile.policies[title] is html_preferred`）を `SourceCompletionProfile.precludes_stage1_ready()`（= いずれかの analyzable field が `html_preferred` か）への **per-field 導出**へ解消。`observed_preferred`/`html_required` は物理存在+妥当性で Stage-1 充足。現行 2 profile では `html_preferred` が `HTML_TITLE_PROFILE.title` のみのため新述語 ≡ 旧 gate → **45 ソース R/H 出力 byte 不変**。DDD: profile が policy 意味論を所有する tell-don't-ask クエリ（passport_builder の `policies[...]`+enum identity への feature envy を解消）。受入は「byte 不変/diff-0」から「導出不変式 + 45 ソース R/H 結果不変」へ再定義（`passport_builder.py` は P3 で編集されるため diff-0 ではない、他 P2-D 保護 path は diff 0）。`branch refactor/source-completion-profile-p3`、コミット `30a75a0e`、unit 1629 / integration 361 green、diff 4 file。

### 4.2 SourceCompletionProfile（policy = StrEnum、DU ではない）

3 frozenset（`html_required_fields` 等）案は「同一フィールドが複数集合に入る矛盾」を再発させるため却下（§9）。フィールド→policy の**全域写像**にして矛盾を型で不能化する。policy 表現は **`StrEnum`**（DU ではない）: 3 variant は payload ゼロ・profile は非永続のため、空マーカー DU は ceremony 増の enum に過ぎない（Wlaschin "when NOT to use a DU"、§9 の YAGNI 基準）。網羅は `match` + `assert_never` で型保証する。

```python
class AnalyzableField(StrEnum):
    title = "title"; body = "body"; published_at = "published_at"

class FieldCompletionPolicy(StrEnum):
    html_required = "html_required"        # 観測値なし前提・HTML 正本・両欠で失敗
    html_preferred = "html_preferred"      # 観測値があっても HTML 正本 (旧 仮タイトル特例)
    observed_preferred = "observed_preferred"  # 観測値が勝ち・HTML は fallback

@dataclass(frozen=True, slots=True)
class SourceCompletionProfile:
    policies: Mapping[AnalyzableField, FieldCompletionPolicy]   # 全 field 必須=全域
    def __post_init__(self) -> None:
        # 全域性を検証後 MappingProxyType でコピー固定 (frozen でも内包 dict は
        # 可変なため実質 immutable 化)。欠落 field は ValueError。
        ...
```

例（Anthropic/ORNL）`HTML_TITLE_PROFILE`: `{title: html_preferred, body: html_required, published_at: observed_preferred}`。
`DEFAULT_PROFILE`: `{title: observed_preferred, body: html_required, published_at: observed_preferred}`。

### 4.3 ObservedArticle（取得事実 VO、JSONB 契約）

`StagedArticleAttributes` を置換する `domain/observed_article.py` の単一値型（`Snapshot` は実装語彙のため除去。pure wire DTO でなく観測事実 VO = Pattern-H passport で、旧 `IncompleteArticle` の後継 = domain 配置が正）。per-field の `{value, origin}` を `ObservedField[T]` generic（Pydantic v2 PEP695 `class ObservedField[T](BaseModel, frozen)`）で表す。**補完状態は持たない**（要否・優先は Profile が決める）。

```python
class ObservedOrigin(StrEnum): feed; sitemap; listing; api   # audit only
class ObservedField[T](BaseModel, frozen): value: T; origin: ObservedOrigin
class ObservedArticle(BaseModel, frozen, populate_by_name):
    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    source_name: SourceName = Field(alias="sourceName")        # required(legacy は repo 注入)
    source_url:  CanonicalArticleUrl = Field(exclude=True)     # required・JSONB 非永続
    title:        ObservedField[str] | None = None
    body:         ObservedField[str] | None = None             # 取れたら保存(None 固定しない)
    published_at: ObservedField[PublishedAt] | None = Field(default=None, alias="publishedAt")
```

JSONB 形（`model_dump(mode="json", by_alias=True)`、`source_url` は除外される）:

```jsonc
{
  "schemaVersion": 1,
  "sourceName": "Anthropic",          // §4.5 Stage2 解決の provenance (Stage1 で焼く)
  "title":       { "value": "Temporary title",          "origin": "sitemap" },
  "body":        null,                                     // 取れなかった = 不在
  "publishedAt": { "value": "2026-05-17T10:00:00+09:00", "origin": "feed" }
}
```

- **取れた事実は全部保存**（原則の徹底）。`passport_builder` の Pattern-H 経路で `fetched.title`/`body`/`published_at` を存在する限り `ObservedField` に詰める。`body=None` 固定はしない（全現行ソースで body=`html_required` のため merge 挙動は不変 = 事実保持・forward-compat。挙動等価表は §7）。
- **`source_url` の二重管理を排除**。`pending_html_articles.url` 列を唯一の authoritative とし JSONB には焼かない（`Field(exclude=True)` でシリアライズを型レベルで常時除外）。in-memory では Stage 1 の passport→enqueue 運搬のため必須。Stage 2 は repository が**全行**で `row.url` 列から `model_validate` 前に raw へ注入（drift を構造的に排除）。
- **`origin` は Source 由来**（`origin=feed` 固定は誤り）。`SourceAdapter.observed_origin` ClassVar がソースごとに宣言（RSS=feed / Anthropic=sitemap / ORNL=listing / hacker_news=api）し `ArticleFetcher`→`passport_builder` が `ObservedField.origin` に stamp。**audit メタで merge を駆動せず**、merge判断はProfileと観測値の有無だけで行う。
- 置き場: `backend/app/collection/domain/`（観測事実 VO。旧 `IncompleteArticle` も domain/。pure wire DTO ではない）。

### 4.4 型を 1 つに畳む — `IncompleteArticle` 削除 + free function 昇格

`IncompleteArticle` は **削除**（改名しない）。`CompletionPendingArticle` は **作らない**：「補完待ち」という lifecycle 状態は `pending_html_articles` 行が表現済で、domain mirror を別に作ると二重表現。Stage 2 へ束ねて渡す物は use-case DTO `ReadyForArticleCompletion`（`pending_id` / `source_id` / `attempt_count` / `observed: ObservedArticle` / `profile` / `source_url`）で足りる。merge は Profile 駆動の **free function** へ移す（リサーチ §1 smart constructor = `AnalyzableArticle` を作る唯一経路）。名前は旧メソッドを継続:

```python
# backend/app/collection/article_completion/promotion.py (新)
def complete_with_html(
    observed: ObservedArticle,
    profile: SourceCompletionProfile,
    html: ExtractedContent | ExtractionEmpty,
    *, source_id: int, source_url: CanonicalArticleUrl,
) -> AnalyzableArticle | ArticleCompletionFailed | ExtractionEmpty:
    # 各 AnalyzableField について profile.policies[field] を match し
    # observed.X.value と html を合成 → AnalyzableArticle factory
    # body=html_required かつ html が ExtractionEmpty → ExtractionEmpty を値返し
    #   (旧 completer.py 短絡を merge 内へ移管、disposition.py 分類は不変)
    # 不足は ArticleCompletionFailed(reason code/detail は domain/completion.py を流用)
```

旧 `incomplete_article.py` の merge ロジックはここへ移植し profile 駆動へ書換。戻り型に `ExtractionEmpty` を追加（現 `completer.py` の短絡を merge 内へ移管、値のまま返し分類は不変）。`source_id` は ingestion FK / `execute` 引数 / pending 列なので型に入れず `ReadyForArticleCompletion` が運ぶ。`match policy ... case _: assert_never` で網羅を型保証。

### 4.5 フロー変更

- **Stage 1**（`pending_enqueue.py`）: `StagedArticleAttributes(... prefer_html_title=...)` をやめ `enqueue(observed: ObservedArticle, *, source_id, ready_at)`。`url=observed.source_url`（列）/ `staged_attributes=observed.model_dump(mode="json", by_alias=True)`（`Field(exclude=True)` で source_url は自動非永続）。`source_fetch/service.py` の match は `case AnalyzableArticle()` / `case ObservedArticle() as observed`。`FetchedArticle.prefer_html_title` flag は撤去し、仮タイトル性は Adapter の `completion_profile`（title=`html_preferred`）が表現。`passport_builder` の Ready gate は **P3 で** `profile.precludes_stage1_ready()`（= いずれかの analyzable field が `html_preferred` か）への per-field 導出化（旧 `force_html_title` title 単独 hardcode を解消、現行 2 profile で新述語 ≡ 旧 gate ＝ 45 ソース byte 不変）。
- **Stage 2 hydrate（ACL）**（`repository.py`）: profile 解決 seam は **`CompletionProfileResolver` Protocol**（`sources/profile_resolver.py`、`resolve`(profile) + `resolve_name`(source_id→SourceName) の 2 メソッド）。repository は `SOURCES` を直接 import せず Protocol にのみ依存（具象 `RegistryCompletionProfileResolver` が `SOURCES` 引き + `news_sources.name` DB fallback を内包、本 ACL は session を持つので self-wire）。`try_load_for_completion`: `raw["source_url"]=str(row.url.root)`（全行・列が authoritative）→ legacy 行（`schemaVersion` 不在）は `raw["sourceName"]=resolver.resolve_name(source_id)` 注入 → `ObservedArticle.model_validate(raw)` → `resolver.resolve(source_id, source_name)` → `ReadyForArticleCompletion(observed, profile, source_url, ...)`。`ArticleCompletionService.execute` は `completer.complete(ready)` 経由で `complete_with_html(ready.observed, ready.profile, html, source_id=, source_url=)` を呼ぶ。Fowler LocalDTO の「構造 mismatch ⇒ mapping 正当」に該当。
- 永続化先 `analyzable_articles` / `AnalyzableArticleRepository.save` は**不変**。

### 4.6 ガードレール（規律として固定）

1. **domain ≠ persistence**。`news_sources`(`NewsSourceORM`) は登録・identity の永続表現、per-source の補完知識はcomposition rootで束縛するcode側の責務とし、混ぜない。P1=既存 `SourceAdapter`/`XxxAdapter` の ClassVar に追加 / P2=`ArticleSource` 集約へ移送 / **P2-D 確定形**=1 `XxxSource` クラスが identity / 補完方針を `ClassVar` 宣言（中間 Adapter 概念は除去）。
2. **lifecycle 非結合**: Stage 2 が `completion_profile` を取得するとき取得 machinery を構築させない。**P2-D 確定形**: `SOURCES: dict[SourceName, ArticleSource]` の値は Source クラスオブジェクトそのもの。resolver は `.completion_profile` を**クラス属性として無 instantiation**で読む。`adapter_factory` / `make_adapter` 自体が不在のため「profile を読むのに machinery を作る」経路が**構造的に不能** = class-ref 構造保証（P1=class-ref 保持 / P2=factory 非呼出 で担保していた契約を、設計から構造へ昇格）。
3. **Stage 2 の Source 同定**: pending 行は `source_id` しか持たない（repository は `news_sources` を join しない）。`source_url` は**全行**で `url` 列が authoritative（JSONB 非永続）。`source_name` は新形 JSONB に焼き、legacy 行のみ `resolver.resolve_name(source_id)` フォールバック（source 出自は最も基本的な観測事実なので provenance として正当）。

---

## 5. 後方互換（in-flight 行）

`pending_html_articles` には旧 `StagedArticleAttributes` 形（`{title, published_at_hint, prefer_html_title}`）の行が残る。pending 行は cron 1 分で claim → 成功時即 DELETE と短命だが、retry backoff で延命しうる。互換は **2 つの責務に分離**する（Fowler LocalDTO / Vernon ACL）:

- **shape 変換 = `ObservedArticle` の before-validator（`_absorb_legacy`、DB 非アクセス）**。`schemaVersion` 不在 **かつ** legacy 専用キー（`prefer_html_title` / `published_at_hint`）の存在で旧形と判定（`schemaVersion` 不在のみだと新規 in-memory 構築を誤認するため不可）。変換: `title→title{value, origin:feed}` / `published_at_hint`(非null)→`publishedAt{value, origin:feed}` / `body`→不在 / `prefer_html_title`→**破棄**（policy は Profile 所有）。**observed shape のみ**触り identity は生成しない。`_absorb_legacy` の旧キー検出文字列（`"prefer_html_title"` / `"published_at_hint"`）は **DB 上の legacy JSONB を識別する wire-key contract のため意図的に残置**（削除した `FetchedArticle.prefer_html_title` flag とは別概念。Commit 4 で `staged_attributes` 列名を残した判断と同型）。
- **identity 注入 = repository(ACL)**。`ObservedArticle` は Optional identity を持たない **strict 型**（`source_url`/`source_name` は required）。`source_url` は全行で `url` 列を `model_validate` 前に raw 注入、legacy 行は `source_name` も `resolver.resolve_name(source_id→news_sources.name)` で注入。
- **profileVersion は持たない**（YAGNI）。retry は最新 policy で解釈するのが正（policy 改定＝「今後こう扱う」の意味）。pending は短命。`schemaVersion`（DTO 形）だけ持つ。
- Alembic data migration は不要（JSONB は読み時変換で吸収、`staged_attributes` は内部 CHECK 無し JSONB、`news_sources`/`analyzable_articles` 不変）。ただしJSONB契約が変わるため、deploy後はworker containerを再起動する。

---

## 6. フェーズ分割

| Phase | 範囲 | 状態 / 備考 |
|---|---|---|
| **P1** | `SourceCompletionProfile` + `FieldCompletionPolicy`(**StrEnum**) + `ObservedArticle`/`ObservedField[T]`（schemaVersion + 互換 before-validator）+ `IncompleteArticle`/`StagedArticleAttributes` **削除** + free function `complete_with_html`(`promotion.py`) + ACL(hydrate, `CompletionProfileResolver` Protocol) 移行 + per-source 知識を既存 `SourceAdapter` ClassVar へ。**Adapter→Source rename しない / Pattern R/H 分岐は温存** | **実装完了**（6 コミット）。単一方向の構造リファクタ。出口契約 `AnalyzableArticle` / `analyzable_articles` 保存は不変。各境界で ruff + pytest + `make test-integration` green |
| **P2(B+C)** | (B) `ArticleSource` **新規集約**（`SourceAdapter` Protocol 改名ではなく追加。Protocol は `collect()` のみへ slim = machinery 契約）+ (C) 継承→委譲（共有基底 4 → 汎用 machinery、継承 12 具象解体、standalone 33 de-ClassVar、`SOURCES: dict[SourceName, ArticleSource]` + 遅延 `adapter_factory`、`ArticleFetcher(source)`） | **実装完了**（`refactor/source-aggregate-p2`、C1-C3 の 3 コミット、merge 済 #545）。**byte 不変**＝出口契約 `AnalyzableArticle`/`analyzable_articles`/`passport_builder`(R/H gate)/`protocol.py`/`domain`/`models`/`alembic`/`schemas` diff 0。各境界で ruff + unit(1635) + `make test-integration`(361) green |
| **P2-D** | **Adapter 概念除去**: `adapter_factory`/`make_adapter`/`SourceAdapter` Protocol 全廃。1 ソース = 1 `XxxSource` クラス（identity/補完方針=`ClassVar`、`collect(cls,tools)`=classmethod、クラスオブジェクト自体が `@runtime_checkable ArticleSource` Protocol を充足）。共有 machinery 4 → `FetchTools` 引数の free function（`multi_feed_rss`/`djangoplicity_entries`/`frontiers_entries`/`mdpi_items`）、具体 Source は `esa\|frontiers\|mdpi/sources.py`。`ArticleFetcher` は `FetchTools`（純 I/O 道具箱）を渡す薄い runner。テスト seam を `fixture_tools` 1 点注入へ | **実装完了**（`refactor/source-collapse-p2d`、C1 加法 / C2 原子 flip / C3 整理 の 3 コミット）。**byte 不変**＝`passport_builder`/`protocol.py`/`domain`/`models`/`alembic`/`schemas`/`promotion`/`source_fetch.service`/`persistence` diff 0。各境界で ruff + unit(1635) + `make test-integration`(361) green、invariant case 数維持 |
| **P3(R/H)** | (R/H) `passport_builder.py` の `force_html_title`（title policy 単独 hardcode）を `SourceCompletionProfile.precludes_stage1_ready()`（any analyzable field が `html_preferred` か）への per-field 導出へ解消（+ `html_required` docstring 誤読防止是正、不変条件テスト 2 本＝分岐駆動/predicate 意味） | **実装完了**（`refactor/source-completion-profile-p3`、`30a75a0e`、code+test 単一コミット）。現行 2 profile で新述語 ≡ 旧 gate ゆえ **45 ソース R/H byte 不変**。受入を「導出不変式 + 45 R/H 結果不変」へ再定義（`passport_builder.py` は本 PR で diff 発生＝もう diff-0 でない、他 P2-D 保護 path は diff 0）。unit 1629 / integration 361 green、diff 4 file |

---

## 7. 検証

- 各コミット境界で `uv run ruff check app/ tests/` + `uv run ruff format --check` + `uv run pytest tests/ -q -m "not integration"` green
- Stage 2 通し: `make test-integration`（pending → claim → complete、**旧/新 JSONB 両形**の hydrate。旧形直接 INSERT → repository identity 注入 + `source_id→name` fallback で Stage2 完走の end-to-end ケースを含む）
- テストは変更箇所の追跡ではなくビジネス不変条件で書く: 「title=`html_preferred` のとき HTML title があれば置換・なければ観測 title 維持」「body=`html_required` で HTML 空（`ExtractionEmpty`）なら `ExtractionEmpty` を値返し → disposition 分類不変」「観測 body があっても html_required で完成 body は HTML 由来（事実保持が merge を変えない）」等
- JSONB契約が変わるため、deploy後はworker containerを再起動する
- 型同期不要（API schema 変更なし。SSoT は触らない）

---

## 8. 影響範囲外（明示）

- `AnalyzableArticle` / `analyzable_articles` / `AnalyzableArticleRepository`: 不変（出口契約）
- API schema (`app/schemas/`) / フロントエンド型: 影響なし（型生成不要）
- Pattern R/H 分岐: P1 + P2(B+C) + P2-D で **byte 不変**（`passport_builder.py` は P2-D まで diff-0）。**P3 で導出化完了**: `force_html_title` → `SourceCompletionProfile.precludes_stage1_ready()` per-field（45 ソース R/H 出力は byte 不変、ただし `passport_builder.py` 自体は P3 で編集＝もう diff-0 でない）
- `news_sources` テーブルスキーマ: 変更なし（`source_type` legacy のまま、Profile は code 側）
- Adapter クラス名 / 継承構造: P1 では不変。P2(B+C) で変更（共有基底 4 → 汎用 machinery、継承 12 具象解体、standalone 33 de-ClassVar、`SourceAdapter` Protocol slim、`ArticleSource` 集約 + `adapter_factory`）。**P2-D で Adapter 概念を完全除去**（`adapter_factory`/`make_adapter`/`SourceAdapter` 全廃、1 source = 1 `XxxSource` クラス、共有 machinery は free function）。いずれも出口契約（`AnalyzableArticle`/`analyzable_articles`/`passport_builder`/`protocol.py`/`domain`/`models`/`alembic`/`schemas`/`promotion`/`source_fetch.service`/`persistence`）は diff 0 で byte 不変
- `pending_html_articles` ORM スキーマ: 不変（`staged_attributes` JSONB の*契約*のみ変化、Alembic 不要）

---

## 9. 決定ログ / 却下案（再議論防止）

| 案 | 却下理由 | 出典/根拠 |
|---|---|---|
| 全 analyzable フィールド `T \| None` の nullable mirror 型 | impossible state（title=None, body≠None 等）を表現可能にする「嘘つき型」。補完後も型が緩く下流再チェックが必要で構造保証が壊れる | リサーチ §1、構造保証原則 |
| per-field discriminated union を**記事**に載せる | 補完状態を instance に散らす Minsky アンチパターン。汎用 `FieldField[T]` は title=NeedsFill 等を再び表現可能にし看板の利点が出ない。本ドメインで2状態/フィールド・非共有・degenerate payload | リサーチ §2/§3 |
| `body: str \| None` を pending 型に持つ | body は補完前に存在しない＝定数。nullable 化は現実より広い型。現行の「body フィールド不在」が唯一の強みでありそれを捨てる | 議論合意 + リサーチ §1 |
| Profile を 3 frozenset で表現 | 同一フィールドが複数集合に入る矛盾を構造的に防げない。全域 policy map なら型で不能化 | リサーチ §2 |
| `profileVersion` を JSONB に保存 | pending 短命、retry は最新 policy 解釈が正。将来要件のないversion固定はpremature | §5 |
| Profile を新 DB テーブル / `source_type` enum 派生 | source_type は audit-only で粗すぎ。per-source 知識は Adapter にあり composition root hardcode が正（Pure DI） | §3.2、per-source設定の単一所有 |
| `FETCHERS` と並走する `SOURCE_COMPLETION_PROFILES` dict | 同一キー・同一寿命の 2 辞書 = desync 余地。`strategy.py` の単一 `SOURCES` registry に同梱し構造的にペア化（`FETCHERS` は `SOURCES` から導出） | 構造保証原則 |
| 名前を素朴に `Source` 単独で ORM と一体化 | `NewsSourceORM` / `SourceName` / `SourceType` / `source_id` と混同。domain と persistence は分離（ガードレール 1） | 目的による責務分離 |

### 9.1 実装確定後の精緻化（設計語彙 → 実装形。再議論防止）

| 設計段階の案 | 確定形と理由 | 根拠 |
|---|---|---|
| `FieldCompletionPolicy` を空マーカー DU（`HtmlRequired \| HtmlPreferred \| ObservedPreferred`） | **`StrEnum`**。3 variant は payload ゼロ・profile は非永続のため DU は ceremony 増の enum に過ぎない。網羅は `match` + `assert_never` で型保証 | Wlaschin "when NOT to use a DU"、YAGNI/構造保証基準 |
| `IncompleteArticle` を `CompletionPendingArticle` へ改名・薄型化 | `IncompleteArticle` は**削除**。`CompletionPendingArticle` は**作らない**（「補完待ち」は `pending_html_articles` 行が表現済 = domain mirror は二重表現）。Stage 2 への束ねは use-case DTO `ReadyForArticleCompletion` で足りる | 二重表現排除、構造保証原則 |
| `ObservedArticleSnapshot`（`persistence/` 配置、`observed` ネスト） | **`ObservedArticle`**（`domain/` 配置、flat）。`Snapshot` は実装語彙。pure wire DTO でなく観測事実 VO = Pattern-H passport（旧 `IncompleteArticle` の後継、それも domain/）。`ObservedField[T]` generic で集約 | リサーチ §4、観測事実 VO は domain |
| `source_url` を JSONB に焼く | `pending_html_articles.url` 列を唯一の authoritative とし JSONB 非永続（`Field(exclude=True)` で型レベル常時除外）。in-memory は Stage1 運搬で必須。drift を構造的に排除 | 二重管理排除、構造保証原則 |
| `origin=feed` 固定 | **Source 由来**（`SourceAdapter.observed_origin` ClassVar が宣言：RSS=feed/Anthropic=sitemap/ORNL=listing/hacker_news=api）。audit only・merge 非駆動 | per-source 知識集約の北極星 |
| 互換 validator が DB アクセス（`source_id→name` も validator 内） | **責務分離**：shape 変換は before-validator（DB 非アクセス）、identity 注入は repository(ACL)。`ObservedArticle` は Optional identity なし strict 型 | Fowler LocalDTO / Vernon ACL |
| 昇格をメソッド `complete()`（`completion.py`） | **free function `complete_with_html`**（`promotion.py`、旧メソッド名継続）。戻り型に `ExtractionEmpty` 追加（旧 `completer.py` 短絡を merge 内へ移管・値返し・分類不変） | smart constructor、責務をファイルで分離 |
| P1 で `Source` 集約化 + Adapter→Source rename | **P1 はしない**（per-source データは既存 `SourceAdapter` ClassVar に追加のみ）。recon で共有基底 4 + 継承 13 が判明：rename 単独は is-a 違反、是正には継承→委譲が不可分（B+C）。機能変更と混ぜず P2 | ユーザー確定、機能/構造変更の分離 |
| P2 で `SourceAdapter`→`ArticleSource` / `XxxAdapter`→`XxxSource` を **Protocol/クラス改名** として実施 | **改名でなく再構成**：`ArticleSource` は**新規 frozen 集約**（identity+policy+`adapter_factory`）。`SourceAdapter` Protocol は **machinery 契約として保持**し `collect()` のみへ slim。`XxxAdapter` は rename せず **de-ClassVar**（取得 machinery として残置）。"Source が adapter を has-a" を rename でなく **集約 + 遅延 factory** で表現 | ユーザー確定（"Adapter を Source の machinery へ降格"）。改名は責務（identity vs 取得）を曖昧化、新規集約が DDD 上正 |
| P2 で (R/H) Profile 導出も同時実施（spec §6 原案） | **P3 へ分離（実装完了）**。B+C は ~73 file の構造移送を **byte 不変**で検証可能に保つのが安全性質。**P3 実装**: 導出は「全 required 観測済→R」案ではなく `precludes_stage1_ready()`（any field `html_preferred`）に確定 — 現行 2 profile で旧 gate と同値ゆえ 45 ソース byte 不変を保ちつつ title hardcode を per-field 一般化。`passport_builder.py` は P3 で diff 発生し受入は「導出不変式 + 45 R/H 結果不変」へ | P1 の機能/構造分離規律と一貫。挙動変化を混ぜない検証を P2-D まで保全し、P3 で gate 定義のみ per-field 化（出力不変） |
| P2 standalone 33 を単一汎用 machinery へ統合 | **しない**（machinery 残置・de-ClassVar のみ）。bespoke parser（Meta AI の AI-tag filter / FierceBiotech strptime / TheRegister link 正規化 / MIC Shift_JIS 等）の強制汎用化は 33 variant の drift リスク。P2 の安全性質は挙動 0。統合は将来の別関心 | 過度の一般化回避、`collect()` body byte 不変 |
| **P2-D 再決定**: P2 で deferred した「`XxxAdapter` 全 collapse / Adapter 概念除去」を最終解消 | **P2-D で collapse 完結**。P2 の「改名でなく再構成」「standalone 33 統合せず」は中間状態（`adapter_factory` 遅延 callable + `SourceAdapter` machinery 契約が残存）だった。P2-D で `adapter_factory`/`make_adapter`/`SourceAdapter` を全廃し 1 source = 1 `XxxSource` クラス（クラスオブジェクト自体が `@runtime_checkable ArticleSource` Protocol を充足）へ最終 collapse。bespoke parser は **個別 `XxxSource` クラスで保持**（汎用化しない＝ P2 の drift 回避判断を継続）、共有 machinery のみ free function 化 | ユーザー確定（北極星「Adapter という中間概念が消えている」へ収束）。外部 importer 不在（strategy/profile_resolver/tasks のみ）を確認、registry 単一 cutover のため strangler 不要 |
| P2-D で `SOURCES: dict[SourceName, type[ArticleSource]]`（`type[]`） | **`dict[SourceName, ArticleSource]`** + instance 形 Protocol をクラスオブジェクトが充足。`type[]` は「実装インスタンスのクラス」に意味が寄り「クラスそのものが Source」の意図とズレる。Protocol 側は `def collect(self, tools)`（`@classmethod` を書かない）、具体は `@classmethod` 実装 | ユーザー確定。クラス属性 = Protocol の instance member をクラスオブジェクトが充足する標準形 |
| P2-D で共有 pipeline を `FetchTools` メソッド化（spec sketch）/ 具体 `XxxSource` を `_common.py` 同居 | **却下**。`FetchTools` は純 I/O 道具箱に留め（`completion_profile`/`ObservedArticle` 昇格判断を持たせない＝責務を混ぜない）、共有 pipeline は `tools` を引数に取る **free function**（L2 道具 / L3 per-source 翻訳の分離・DDD 規律と一致）。具体 Source は `esa\|frontiers\|mdpi/sources.py` へ外出し、`_common.py` は共通処理のみ | ユーザー確定（FetchTools の god-object 再発回避、source-specific 事実を common file に残さない） |

---

## 10. 出典

- Alexis King, "Parse, don't validate" — https://lexi-lambda.github.io/blog/2019/11/05/parse-don-t-validate/
- Scott Wlaschin, "Designing with types: Making illegal states unrepresentable" — https://fsharpforfunandprofit.com/posts/designing-with-types-making-illegal-states-unrepresentable/
- Scott Wlaschin, "Designing with types: Representing states" — https://fsharpforfunandprofit.com/posts/designing-with-types-representing-states/
- Yaron Minsky, "Effective ML Revisited" (Jane Street) — https://blog.janestreet.com/effective-ml-revisited/
- Cliffle, "The Typestate Pattern in Rust" — https://cliffle.com/blog/rust-typestate/
- Martin Fowler, "LocalDTO" — https://martinfowler.com/bliki/LocalDTO.html
- Vaughn Vernon, *Implementing Domain-Driven Design* (Anti-Corruption Layer) — https://www.dddcommunity.org/book/implementing-domain-driven-design-by-vaughn-vernon/

---

## 11. 次の一手

- **P1: 完了**（6 コミット、`main` merge 済）。
- **P2(B+C): 完了**（`refactor/source-aggregate-p2`、C1-C3 の 3 コミット。本 spec は実装確定形へ整合済 = 記録 SSoT）。
  1. (B) `ArticleSource` 新規集約 + `SourceAdapter` Protocol を `collect()` のみへ slim（machinery 契約）。
  2. (C) 共有基底 4 → 汎用 machinery、継承 12 具象解体（NASA/Cornell config の module 化・thin subclass 10 file 削除）、standalone 33 de-ClassVar、`SOURCES: dict[SourceName, ArticleSource]` + 遅延 `adapter_factory`、`ArticleFetcher(source)`。
  3. byte 不変＝出口契約 / `passport_builder`(R/H gate) / `protocol.py` / `domain` / `models` / `alembic` / `schemas` diff 0。ruff + unit(1635) + `make test-integration`(361) green。
- **P2-D: 完了**（`refactor/source-collapse-p2d`、C1 加法 / C2 原子 flip / C3 整理 の 3 コミット。本 spec は実装確定形へ整合済 = 記録 SSoT）。
  1. (C1) `tools/fetch_tools.py`（純 I/O 道具箱 `FetchTools`）+ テスト seam `_fixture_tools.py` を加法追加（既存 file 不変）。
  2. (C2) `ArticleSource` を frozen dataclass → `@runtime_checkable` Protocol へ原子置換、`SourceAdapter`/`adapter_factory`/`make_adapter` 全廃、45 ソースを `XxxSource` クラス（identity/補完方針=`ClassVar`、`collect`=classmethod）へ、共有 machinery 4 → free function（具体 Source は `esa\|frontiers\|mdpi/sources.py`）、`strategy.py` class-ref registry、`ArticleFetcher` 薄 runner、テスト 21 file を `fixture_tools` へ。
  3. (C3) 識別子/docstringからAdapterを一掃し、specと実装を整合。
  4. byte 不変＝`passport_builder` / `protocol.py` / `domain` / `models` / `alembic` / `schemas` / `promotion` / `source_fetch.service` / `persistence` diff 0。各境界で ruff + unit(1635) + `make test-integration`(361) green、invariant case 数維持。
- **P3（実装完了・別 PR）= (R/H) のみ**: `passport_builder.py` の `force_html_title`（title policy 単独 hardcode）を `SourceCompletionProfile.precludes_stage1_ready()`（any analyzable field が `html_preferred` か）への per-field 導出へ解消。`observed_preferred`/`html_required` は物理存在+妥当性で Stage-1 充足。現行 2 profile で新述語 ≡ 旧 gate のため **45 ソース R/H byte 不変**（出力反転なし＝gate 定義の per-field 化）。`html_required` docstring を誤読防止是正（挙動不変、Stage-2 HTML 正本は維持）、不変条件テスト 2 本追加（passport_builder=分岐駆動 / domain=predicate 意味）。`branch refactor/source-completion-profile-p3`、`30a75a0e`、unit 1629 / integration 361 green、diff 4 file。受入は「byte 不変/diff-0」→「導出不変式 + 45 R/H 結果不変」へ再定義（本 PR で `passport_builder.py` に diff 発生、他 P2-D 保護 path は diff 0）。deploy後はworker containerを再起動する。
- deploy順: P2-Dコード投入後にworker containerを再起動する。`app/schemas/`不変のため型生成は不要。
