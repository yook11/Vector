# Gemini クライアント層リファクタリング

## 0. 最上位の目的

**レート制限の設定漏れが起きない仕組みにする。**

設定漏れによって本番で 429 が出る事態を構造的に防ぐ。

### 設計要件

| # | 要件 | 判定基準 |
|---|---|---|
| 1 | モデル差し替えがスムーズ | 差し替え = クラス差し替え。RPM/RPD はクラス定義の一部として必ず存在する |
| 2 | 操作ごとにモデルと上限が co-locate | 分析の設定は分析クラスだけ、embedding の設定は embedding クラスだけを見れば済む |
| 3 | 設定漏れが起動前に検出可能 | 定義がなければ動かない（abstractproperty / クラス定数の必須化） |

### 既存原則（前提として守る）

- レート制限の事情は Gemini クライアント層より上に漏れない
- 動作環境（worker 数、並列度）を変えてもレート制限が正しく守られる

---

## 1. 現状の問題点

### 1.1 リトライロジックの3重複（GeminiEmbedder）

`embed_batch` / `embed_query` / `embed` に同一のリトライループが存在（各50行超）。

違いは実質3箇所のみ:

| 差分 | embed_batch | embed_query | embed |
|---|---|---|---|
| `contents` に渡す型 | `texts: list[str]` | `text: str` | `text: str` |
| `task_type` | `RETRIEVAL_DOCUMENT` | `RETRIEVAL_QUERY` | `RETRIEVAL_DOCUMENT` |
| 戻り値の取り出し | `[e.values for e in ...]` | `embeddings[0].values` | `embeddings[0].values` |

#### 実害

- **修正コスト3倍** — レート制限判定の変更、ログ項目追加、防御ロジック追加が3箇所に必要
- **既にズレ始めている** — `embed_batch` にだけ `batch_size` ログあり、`embed` はログ suffix 不統一
- **テストも3倍** — リトライ挙動のテストを3メソッド分書く必要がある

### 1.2 プロバイダ固有知識の散在

- `_is_rate_limit_error()` で Gemini SDK の `ClientError(code=429)` を string match 含みで判定
- リトライパラメータ（`MAX_RETRIES`, `RATE_LIMIT_DELAY` 等）がモジュールグローバル定数
- プロバイダ追加時、リトライ・ログ・エラー判定をすべて再実装する構造

### 1.3 エラー対処方針が手続きに埋没

「どのエラーにどう対処するか」がリトライループの条件分岐に散らばっている。
対処方針の全体像を把握するにはループ全体を読む必要がある。

### 1.4 Batch API のレートリミット問題（修正済み・未コミット）

`embed()` が `embed_batch([text])` に委譲 → batchEmbedContents (15 RPM) を消費。
→ `contents=text`（文字列直渡し）で embedContent (500 RPM) にルーティングするよう修正済み。

### 1.5 レート制限がプロセスを跨いで効かない

Taskiq worker が 2プロセス × 100 async tasks = 最大200並列で動作。
Python オブジェクト（asyncio.Lock, 変数）ではプロセス間でカウント共有できず、
各プロセスが独立にカウント → 合算でクォータ超過。

### 1.6 レート制御が間違ったレイヤーにある

`ai_analyzer.py` の `REQUEST_INTERVAL=4.0s` は Service 層にレート制御が漏れている。
パイプラインがタスク単位で並列実行するとバイパスされる。
本来 Gemini クライアント層が自律的にスロットルすべき。

### 1.7 モデル設定が config.py に散在

`GeminiAnalyzer` の `model_name` が `settings.ai_model_name` を参照。
モデル差し替え時に config.py とクラスの両方を確認する必要があり、
RPM/RPD の設定漏れが起きやすい構造。

---

## 2. 設計仕様

### 2.1 レート制限の構造

#### レイヤー図

```
┌─────────────────────────────────────────┐
│ Pipeline / Service 層                    │ ← レート制限を知らない
│  「分析して」「embedding して」            │   呼べば適切にスロットルされる
├─────────────────────────────────────────┤
│ Gemini クライアント層                     │ ← ★レート制限の知識はここに閉じる★
│  GeminiAnalyzer / GeminiEmbedder        │   MODEL, RPM, RPD をクラス定数で持つ
│  API 呼び出し前に RateLimiter.acquire()   │
├─────────────────────────────────────────┤
│ RateLimiter (汎用インフラ)               │ ← Gemini を知らない
│  Redis ZSET sliding window              │   key, max_requests, window を受け取るだけ
└─────────────────────────────────────────┘
```

#### モデル設定の co-location と明示的宣言の強制

モデル名・RPM・RPD はクラス定数としてクラスに co-locate する。
config.py に残すのは API キー（環境ごとに変わる秘匿値）のみ。

基底クラスで `ClassVar` を宣言し、サブクラスでの定義を強制する:

```python
class BaseEmbedder(ABC):
    MODEL: ClassVar[str]           # 未定義なら TypeError で起動失敗
    DIMENSION: ClassVar[int]
    RPM: ClassVar[int | None]      # None = 制限なし（明示的な選択）
    RPD: ClassVar[int | None]      # None = 制限なし（明示的な選択）
```

「制限がない」と「設定を忘れた」は意味が全く違う:

| 状態 | 意味 | 正しいか |
|---|---|---|
| `RPM = 500` | 500 RPM で制限する | 意識的な設定 |
| `RPM = None` | 制限しない | 意識的な選択 |
| RPM 未定義 | 忘れた | バグ。起動時に落ちるべき |

`RPM = None` を書く行為自体が「このプロバイダのクォータを調べた上で、
制限不要と判断した」というレビュー可能な意思表示になる。

サブクラスでの実装例:

```python
class GeminiEmbedder(BaseEmbedder):
    MODEL = "gemini-embedding-001"
    DIMENSION = 768
    RPM = 500
    RPD = 1500

class GeminiAnalyzer(BaseAnalyzer):
    MODEL = "gemini-2.5-flash-lite"
    RPM = 50
    RPD = 1500
```

モデル差し替え = クラスの定数を変更。RPM/RPD が隣にあるので一緒に変わる。

#### RateLimiter の注入

RateLimiter は振る舞いを持つオブジェクトとしてコンストラクタ注入する。
RPM の**値**ではなく**制御**を注入する。

```python
class GeminiEmbedder(BaseEmbedder):
    MODEL = "gemini-embedding-001"
    DIMENSION = 768
    RPM = 500
    RPD = 1500

    def __init__(self, rpm_limiter: RateLimiter, rpd_limiter: RateLimiter) -> None:
        ...
```

ファクトリ（composition root）がクラス定数を読んで RateLimiter を組み立てる。
`RPM = None` / `RPD = None` の場合はリミッタを生成しない（NoopLimiter または None）:

```python
def get_embedder() -> BaseEmbedder:
    redis = get_redis()
    embedder_cls = GeminiEmbedder  # 差し替え点
    return embedder_cls(
        rpm_limiter=_build_limiter(redis, embedder_cls.MODEL, "rpm", embedder_cls.RPM, 60),
        rpd_limiter=_build_limiter(redis, embedder_cls.MODEL, "rpd", embedder_cls.RPD, 86400),
    )

def _build_limiter(redis, model, kind, limit, window) -> RateLimiter | None:
    if limit is None:
        return None
    return RateLimiter(redis, f"ratelimit:{model}:{kind}", limit, window)
```

#### RPM vs RPD の枯渇時挙動

| 制限 | window | 枯渇時の挙動 | 根拠 |
|---|---|---|---|
| RPM | 60秒 | 空きが出るまで待つ | 数秒で回復する |
| RPD | 86400秒 | 即座に `DailyQuotaExhaustedError` を raise | 日が変わるまで回復しない |

### 2.2 RateLimiter 実装方式

Redis ZSET sliding window。

- キー: `ratelimit:{model}:{rpm|rpd}`
- メンバー: タイムスタンプ（またはユニーク ID）
- スコア: タイムスタンプ
- `acquire()` 時: window 外のエントリを ZREMRANGEBYSCORE で削除 → ZCARD で現在数を確認
- RPM: 上限に達していたら sleep して再チェック
- RPD: 上限に達していたら即 raise

プロセス数・worker 数が変わっても正確に制限を守れる。
Vector は既に Redis（taskiq broker）を使っており、追加インフラ不要。

配置: `app/utils/rate_limiter.py`（Gemini 固有ではない汎用インフラ）

### 2.3 エラー階層

Embedder と Analyzer で共通の構造:

```
# Embedder 側
EmbeddingError              # 基底: 分類不能・最終失敗
├── RateLimitError          # 429: レート制限（RPM）
├── DailyQuotaExhaustedError # 429: 日次クォータ枯渇（RPD）
├── TransientError          # 5xx・通信エラー・タイムアウト
└── InvalidInputError       # 4xx (429以外): 入力起因の永続的エラー

# Analyzer 側（同構造）
AnalysisError
├── AnalysisRateLimitError
├── AnalysisDailyQuotaExhaustedError
├── AnalysisTransientError
└── AnalysisInvalidInputError
```

注: `DailyQuotaExhaustedError` は RateLimiter が raise する。
API の 429 レスポンスとは別に、ローカルの RPD リミッタが事前に検出する。

### 2.4 エラー種別ごとの対処

| エラー | 対処 | 根拠 |
|---|---|---|
| RateLimitError | 固定ディレイで待機後に再試行（最大1回） | こちらの呼びすぎ。待てば通る |
| DailyQuotaExhaustedError | 即時 raise | 日が変わるまで回復しない |
| TransientError | 指数バックオフで再試行（最大3回） | サーバ側の一時的不調 |
| InvalidInputError | 即時 raise | 入力が間違い。リトライしても同じ |
| 基底 Error | 即時 raise | 未知のエラーをリトライするとバグを隠す |

レート制限の再試行カウントは通常リトライ予算と独立管理。

---

## 3. BaseEmbedder リファクタリング

### 3.1 存在意義

BaseEmbedder は、テキストからベクトルへの変換操作をドメインに提供する抽象。
上流は「テキストを embedding に変換する」操作だけを知り、
プロバイダ（Gemini, OpenAI, ...）を知らない。

### 3.2 責務

**担うこと:**
- テキストから embedding ベクトルへの変換操作の提供
- プロバイダ呼び出し時の失敗への対処方針の一元的適用（リトライ・バックオフ）
- RateLimiter による事前スロットリング
- プロバイダ固有の例外をドメインエラーに翻訳する境界の提供

**担わないこと:**
- embedding の永続化（Service / Repository の責務）
- 入力テキストの前処理・後処理（呼び出し側の責務）

### 3.3 提供する操作

| メソッド | 対象 | task_type | 用途 |
|---|---|---|---|
| `embed_document(text)` | 単一文書 | RETRIEVAL_DOCUMENT | パイプラインの逐次 embedding |
| `embed_documents(texts)` | 複数文書 | RETRIEVAL_DOCUMENT | バッチ embedding |
| `embed_query(text)` | 検索クエリ | RETRIEVAL_QUERY | セマンティック検索 |

### 3.4 クラス構造 (Template Method)

```
BaseEmbedder（フロー制御）
├── embed_document(text)       → concrete: _embed_with_retry(text, DOCUMENT) → [0]
├── embed_documents(texts)     → concrete: _embed_with_retry(texts, DOCUMENT)
├── embed_query(text)          → concrete: _embed_with_retry(text, QUERY) → [0]
├── _embed_with_retry(...)     → concrete: RateLimiter.acquire() + リトライ + _call_api + _translate_error
├── _call_api(...)             → abstract: SDK 呼び出しだけ
└── _translate_error(exc)      → abstract: 例外翻訳だけ

GeminiEmbedder（フック提供 + モデル設定）
├── MODEL = "gemini-embedding-001"
├── DIMENSION = 768
├── RPM = 500
├── RPD = 1500
├── _call_api(contents, task_type)   → str なら embedContent、list なら batchEmbedContents
└── _translate_error(exc)            → ClientError(429) → RateLimitError 等
```

`_embed_with_retry` 内で contents の型（str / list）を見て
RPM リミッタ（embedContent 用）と batch リミッタ（batchEmbedContents 用）を選択する。

### 3.5 プロバイダ拡張時のサブクラスの責務

**必ず書くもの（未定義なら起動失敗）:**
- `MODEL`, `DIMENSION` — モデル固有の事実
- `RPM`, `RPD` — レート制限。不要なら `None` を明示的に設定
- `_call_api()` — SDK 呼び出し
- `_translate_error()` — 例外翻訳

**書かないもの:** リトライロジック、ディレイ計算、再試行回数管理、エラー対処方針、RateLimiter 管理

### 3.6 呼び出し側のメソッド名変更

| 現在 | 変更後 | ファイル |
|---|---|---|
| `embedder.embed(text)` | `embedder.embed_document(text)` | `pipeline_tasks.py` |
| `embedder.embed_batch(texts)` | `embedder.embed_documents(texts)` | `embedding.py` |
| `embedder.embed_query(text)` | `embedder.embed_query(text)` | `embedding.py`（変更なし） |

---

## 4. レイヤー違反の解消

### 4.1 ai_analyzer.py の REQUEST_INTERVAL 削除

`ai_analyzer.py:21` の `REQUEST_INTERVAL = settings.analysis_request_interval` と
`ai_analyzer.py:219` の `await asyncio.sleep(REQUEST_INTERVAL)` を削除。

レート制御は GeminiAnalyzer 内の RateLimiter に移管済みのため不要。

### 4.2 config.py からモデル設定を整理

モデル名（`ai_model_name`）はクラス定数に移動。
config.py に残すもの: API キー、バッチサイズ等の運用パラメータ。
