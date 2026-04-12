# BaseEmbedder リファクタリング

## 1. 現状の問題点

### 1.1 リトライロジックの3重複

`GeminiEmbedder` の `embed_batch` / `embed_query` / `embed` の3メソッドに、
ほぼ同一のリトライループが存在する（各50行超）。

違いは実質3箇所のみ:

| 差分 | embed_batch | embed_query | embed |
|---|---|---|---|
| `contents` に渡す型 | `texts: list[str]` | `text: str` | `text: str` |
| `task_type` | `RETRIEVAL_DOCUMENT` | `RETRIEVAL_QUERY` | `RETRIEVAL_DOCUMENT` |
| 戻り値の取り出し | `[e.values for e in ...]` | `embeddings[0].values` | `embeddings[0].values` |

それ以外（リトライループ・例外分岐・レート制限処理・指数バックオフ・最終raise）は
3つとも同一コードのコピー。

#### 実害

- **修正コスト3倍** — レート制限判定の変更、ログ項目追加、防御ロジック追加が3箇所に必要
- **既にズレ始めている** — `embed_batch` にだけ `batch_size` ログあり、`embed` はログ suffix 不統一
- **テストも3倍** — リトライ挙動のテストを3メソッド分書く必要がある

### 1.2 プロバイダ固有知識の散在

- `_is_rate_limit_error()` で Gemini SDK の `ClientError(code=429)` を string match 含みで判定
- リトライパラメータ（`MAX_RETRIES`, `RATE_LIMIT_DELAY` 等）がモジュールグローバル定数
- プロバイダ追加時、リトライ・ログ・エラー判定をすべて再実装する構造

### 1.3 エラー対処方針が手続きに埋没

リトライループ内の条件分岐でエラー対処を実装:

```python
if _is_rate_limit_error(e):       # 分類
    rate_limit_retries += 1        # 独立カウント管理
    await asyncio.sleep(...)       # 固定ディレイ
    attempt -= 1                   # 予算消費しない
    continue
else:
    # 指数バックオフ              # 別の戦略
    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
    await asyncio.sleep(delay)
```

「どのエラーにどう対処するか」が手続きの中に散らばっており、
対処方針の全体像を把握するにはループ全体を読む必要がある。

### 1.4 Batch API のレートリミット問題（発見済み・修正済み）

`embed()` が `embed_batch([text])` に委譲していたため、1件のembeddingでも
SDK が `batchEmbedContents` API (15 RPM) にルーティングされていた。
`embedContent` (500 RPM) を使うべきところを誤って低枠APIを消費。
→ `contents=text`（文字列）を直接渡すよう修正済み（未コミット）

## 2. 設計仕様

### 2.1 存在意義

BaseEmbedder は、テキストからベクトルへの変換操作をドメインに提供する抽象。
上流は「テキストを embedding に変換する」操作だけを知り、
プロバイダ（Gemini, OpenAI, ...）を知らない。

### 2.2 責務

**担うこと:**
- テキストから embedding ベクトルへの変換操作の提供
- プロバイダ呼び出し時の失敗への対処方針の一元的適用
- プロバイダ固有の例外をドメインエラーに翻訳する境界の提供

**担わないこと:**
- embedding の永続化（Service / Repository の責務）
- 入力テキストの前処理・後処理（呼び出し側の責務）

### 2.3 提供する操作

| メソッド | 対象 | task_type | 用途 |
|---|---|---|---|
| `embed_document(text)` | 単一文書 | RETRIEVAL_DOCUMENT | パイプラインの逐次 embedding |
| `embed_documents(texts)` | 複数文書 | RETRIEVAL_DOCUMENT | バッチ embedding |
| `embed_query(text)` | 検索クエリ | RETRIEVAL_QUERY | セマンティック検索 |

`embed_queries` は提供しない（検索クエリは性質上常に1つずつ）。

### 2.4 エラー階層

```
EmbeddingError              # 基底: 分類不能・最終失敗
├── RateLimitError          # 429: レート制限
├── TransientError          # 5xx・通信エラー・タイムアウト
└── InvalidInputError       # 4xx (429以外): 入力起因の永続的エラー
```

エラーは「何が起きたか」の分類のみ。「どう対処するか」は BaseEmbedder の責務。

### 2.5 エラー種別ごとの対処

| エラー | 対処 | 根拠 |
|---|---|---|
| RateLimitError | 固定ディレイで待機後に再試行（最大1回） | こちらの呼びすぎが原因。待てば通る |
| TransientError | 指数バックオフで再試行（最大3回） | サーバ側の一時的不調。時間で回復 |
| InvalidInputError | 即時 raise | 入力が間違い。リトライしても同じ |
| EmbeddingError (基底) | 即時 raise | 未知のエラーをリトライするとバグを隠す |

レート制限の再試行カウントは通常リトライ予算と独立管理。

### 2.6 クラス構造 (Template Method)

```
BaseEmbedder（フロー制御）
├── embed_document(text)       → concrete: _embed_with_retry(text, DOCUMENT) → [0]
├── embed_documents(texts)     → concrete: _embed_with_retry(texts, DOCUMENT)
├── embed_query(text)          → concrete: _embed_with_retry(text, QUERY) → [0]
├── _embed_with_retry(...)     → concrete: リトライ + _call_api + _translate_error
├── _call_api(...)             → abstract: SDK呼び出しだけ
└── _translate_error(exc)      → abstract: 例外翻訳だけ

GeminiEmbedder（フック提供のみ）
├── _call_api(contents, task_type)   → str なら embedContent、list なら batchEmbedContents
└── _translate_error(exc)            → ClientError(429) → RateLimitError 等
```

### 2.7 プロバイダ拡張時のサブクラスの責務

**書くもの:** `_call_api()`, `_translate_error()`, `dimension`, `provider_name`
**書かないもの:** リトライロジック、ディレイ計算、再試行回数管理、エラー対処方針

### 2.8 呼び出し側のメソッド名変更

| 現在 | 変更後 | ファイル |
|---|---|---|
| `embedder.embed(text)` | `embedder.embed_document(text)` | `pipeline_tasks.py` |
| `embedder.embed_batch(texts)` | `embedder.embed_documents(texts)` | `embedding.py` |
| `embedder.embed_query(text)` | `embedder.embed_query(text)` | `embedding.py`（変更なし） |
