# Stage 2 (Classification) DeepSeek-V4-Flash 移行

> ステータス: 設計確定 + 実機 PoC 検証済（実装待ち）。最終更新 2026-04-28。

## 概要

現行の Stage 2 分類処理 (`backend/app/analysis/classifier/gemini.py`、`gemini-2.5-flash-lite`) を DeepSeek-V4-Flash に乗り換える。Stage 1 (extraction) は Gemini のまま据え置き、本移行のスコープ外。

## 動機

- コスト削減: Gemini 2.5 Flash Lite 比でオーダーレベルの単価低減（$0.14/M input cache miss / $0.28/M output、cache hit はさらに 1/50）
- DeepSeek 採用方針 (`memory: project_vector_agent_features.md`) との整合 — 週次ダイジェストでも DeepSeek を採用予定で、Adapter 基盤の共通化が進む
- 既存の `BaseClassifier` テンプレート + factory 抽象化により、上位レイヤを変えずに移行できることを既存実装で確認済

## 確定事項サマリ

| # | 論点 | 決定 |
|---|---|---|
| 1 | データ送信ポリシー | Stage 2 入力 (`title_ja` + `summary_ja`、AI 翻訳済み) のみ送信 |
| 2 | 移行スコープ | Stage 2（分類）のみ。Stage 1 は Gemini のまま |
| 3 | モデル | DeepSeek-V4-Flash (`deepseek-v4-flash`) |
| 4 | JSON 型強制方式 | Function Calling + `strict: true` (beta endpoint)、**inline flat schema 必須**（PoC で確定） |
| 5 | Thinking モード | `disabled` 固定 |
| 6 | 既存 Gemini Classifier | factory に残し、4 週間安定運用後にクリーンアップ PR |
| 7 | リリース戦略 | スクリプト型 shadow run → env 切替 |
| 8 | コスト上限（最低限の防御） | `max_tokens=512` + `summary_ja` 8000 chars truncate + HTTP 402 を fail fast |

残高アラート / 監視基盤は本 spec のスコープ外（独立 PR、別 spec）。

## スコープ / 非スコープ

### スコープ
- DeepSeekClassifier (Function Calling + strict mode + inline schema) の新規実装
- factory への provider 分岐追加
- `config.py` への `deepseek_api_key` 追加
- AI 境界用の手書き flat な strict 互換 JSON Schema 定数 (`CLASSIFICATION_TOOL_SCHEMA`) 新設
- Pydantic schema (`ClassificationRawResponse`) と tool schema の整合性検証テスト
- shadow run 用の一時スクリプト
- 切替ドキュメント

### 非スコープ
- Stage 1 (extraction) の差し替え
- 残高プローブ cron / 通知基盤 / Logfire 共通基盤（独立 PR）
- DeepSeek コンソール側の運用設定（運用作業として別管理）
- Anthropic 等の追加プロバイダ

## アーキテクチャ概要

既存の Adapter パターンを温存。Service / Task / Domain / Repository は SDK 非依存のまま、`backend/app/analysis/classifier/deepseek.py` を新規追加して factory で分岐するのみ。

```
ClassificationService (SDK 非依存)
        │
        ▼
ClassifierFactory (settings.ai_provider)
        ├── GeminiClassifier (既存、cold standby)
        └── DeepSeekClassifier (新規)
                │
                ▼
        OpenAI SDK (base_url=https://api.deepseek.com/beta)
        Function Calling + strict: true + inline schema
```

## PoC 検証結果（2026-04-28）

`backend/scripts/poc_deepseek_classifier.py` で実機検証した重要な発見。

### 検証内容と結果

| Step | schema 形式 | 結果 |
|---|---|---|
| 4 | Pydantic 標準出力（`$defs` + `$ref` 形式） | **2/2 件で Pydantic 検証失敗**。`category` が enum 値外（`NEW_TECHNOLOGY`/`ENTERTAINMENT` 等）、`topic` が pattern 違反（日本語、大文字、ハイフン） |
| 5 | inline schema（`$ref` 展開済み、enum/pattern を properties に直書き） | **2/2 件で完全合格**。`category: "ai"`、`topic: "gpt 6"` 等、すべて正規化済み出力 |

### 確定した事実

1. **DeepSeek strict mode は `$ref`/`$defs` 経由の制約を enforce しない** — AI が `$defs` を辿らず無視する
2. **inline schema（enum/pattern を properties に直書き）であれば完全に enforce される**
3. 公式ドキュメントの「strictly adheres to JSON Schema」記載は `$ref` 形式では成立しない。spec の前提を実機ベースで補正

### 影響

- 当初想定していた「Pydantic の `model_json_schema()` 出力をそのまま `strict: true` の `parameters` に渡す」案は **不可**
- AI 境界 schema は **手書き定数として `schema_tool.py` に持つ**ことを PR-A の必須要件として確定
- Pydantic schema (`ClassificationRawResponse`) との整合性ドリフトが新たなリスクとなるため、テストで構造的に検証する

## 各論点の根拠

### 1. データ送信ポリシー (Stage 2 入力のみ)

- Stage 1 入力は元記事本文 (第三者著作物)。DeepSeek 規約懸念があるため送らない
- Stage 2 入力は AI 翻訳済みの `title_ja` + `summary_ja`。原文そのものではないため許容範囲と判断
- 将来本文を LLM に渡すユースケースが出た場合は、その箇所のみ Anthropic / Gemini を使う方針で `project_vector_agent_features.md` と整合

### 2. 移行スコープ (Stage 2 のみ)

- 上記ポリシーから論理的に決まる
- Stage 1 は Gemini のまま据え置き

### 3. モデル (V4-Flash)

- 分類タスクは軽量、Pro の高度推論は不要
- 公式・業界推奨が一致 (「Flash is the correct option for high-QPS classification」)
- 単価最安 ($0.14/$0.28 per M tokens、cache hit 込みでさらに 1/10)

### 4. JSON 型強制 (Function Calling + strict mode beta + inline schema)

DeepSeek の出力強制 4 階層を比較した結果（PoC 検証済）:

| 方式 | スキーマ準拠率 | 採否 |
|---|---|---|
| **Function Calling + `strict: true` + inline schema (beta)** | **PoC で 100%（2/2 件で完全合格）** | **採用** |
| Function Calling + `strict: true` + `$ref` 形式 | PoC で 0%（2/2 件で違反） | **却下**（Pydantic 標準出力は使えない） |
| Function Calling 通常版 | 〜85% | 却下 |
| `response_format={"type":"json_object"}` (JSON mode) | < 85%、空 content リスクあり | 却下 |
| `response_format={"type":"json_schema"}` | API 未対応 (`json_schema is unavailable now`) | 却下 |

採用条件:
- `base_url="https://api.deepseek.com/beta"` を使う
- 各 function に `"strict": true`
- **JSON Schema は inline flat 形式**（`$ref`/`$defs` 不可、enum/pattern を properties に直書き）
- `additionalProperties: false`、全 property を `required`、subset 外制約（`minLength`/`maxLength` 等）は schema には書かず受信後 Pydantic で再検証

### 5. Thinking モード (disabled)

- 単価は同じだが、Thinking 有効時は reasoning trace が output token として課金 → 実コスト増
- Stage 2 はシンプル分類タスク、出力フォーマットは strict + inline schema で構造保証済 → Thinking で得る精度向上の余地が薄い
- Vector の精度問題（`memory: project_prompt_simplification_plan.md`）はプロンプト改善で解く方針が既決
- Non-thinking で TTFT 300〜500ms、Thinking 有効時は 0.98s〜1.04s

### 6. 既存 Gemini Classifier の扱い

- factory に残しコールド化 (`AI_PROVIDER=gemini` で即時呼び戻し可能)
- 100% 切替後 4 週間の安定運用を確認後、Gemini classifier・テスト・`google-genai` 依存（Stage 1 で残す場合は要検討）を削除する PR を別途出す
- フェイルオーバー常時待機案は不採用 (`feedback_failure_visibility` / `feedback_verify_before_fallback` / `feedback_business_value_investment` と矛盾するため)

### 7. リリース戦略 (shadow run スクリプト → env 切替)

- 評価サンプル 25 件 (PR #133 の reclassify 由来) + 直近 100 件で両 classifier を呼んで比較
- スクリプトは `scripts/compare_classifiers.py` に一時的に置き、本番コードに侵食させない
- 結果は `discussions/` に markdown レポートで残す
- 判定基準クリアを確認後、`AI_PROVIDER=deepseek` に切替 + worker 再起動 (`memory: feedback_worker_restart_after_orm_change.md`)

判定基準:

| 指標 | 閾値 |
|---|---|
| カテゴリ一致率 | Gemini 比 ±5% 以内 |
| Pydantic 検証失敗率 | < 1% |
| per-call レイテンシ | Gemini 比 1.5× 以内 |

### 8. コスト上限 (本 spec の最低限の防御)

| 項目 | 値 | 配置 |
|---|---|---|
| Per-call output キャップ | `max_tokens=512` | `DeepSeekClassifier._call_api` |
| Per-call input キャップ | `summary_ja` 8000 chars truncate | `DeepSeekClassifier._call_api` |
| HTTP 402 (Insufficient Balance) | `InsufficientBalanceError` で fail fast、taskiq 非リトライ | `DeepSeekClassifier._translate_error` |
| Kill switch | `AI_PROVIDER=gemini` env 切替 (論点 6 で確保) | 既存 factory |

残高プローブ / 通知 / Logfire 観測は別 PR (独立した「DeepSeek 運用基盤」spec)。

## Vector 既存設計との整合チェック

| メモ | 整合性 |
|---|---|
| `feedback_structural_guarantee.md` | strict mode + inline schema で出力フォーマットを構造的に保証、`max_tokens` で per-call 上限を構造強制、tool schema と Pydantic の整合をテストで強制 |
| `feedback_failure_visibility.md` | shadow レポートで精度可視化、HTTP 402 を fail fast、フェイルオーバー隠蔽なし |
| `feedback_verify_before_fallback.md` | shadow run で実データ検証してから切替、PoC で実機検証済 |
| `feedback_business_value_investment.md` | feature flag 段階展開・常時フェイルオーバーなど過剰投資を回避 |
| `feedback_responsibility_by_purpose.md` | 残高監視を独立 PR に分離、AI 境界 schema をドメイン schema と別ファイル化 |
| `feedback_worker_restart_after_orm_change.md` | env 切替後の worker 再起動を手順に明記 |
| `feedback_commit_pr_language.md` | PR / コミットは日本語、Conventional Commits の type/scope のみ英語 |

## AI 境界 schema の取り扱い

### 方針確定（PoC ベース）

DeepSeek strict mode は `$ref`/`$defs` 経由の制約を enforce しないため、Pydantic の `model_json_schema()` 出力をそのまま渡すことはできない。AI 境界 schema は **手書き flat な JSON Schema 定数**として `schema_tool.py` に持つ。

```
backend/app/analysis/classifier/
├── schema.py          ← ClassificationRawResponse (受信後検証用、変更しない)
└── schema_tool.py     ← CLASSIFICATION_TOOL_SCHEMA (新規、AI 境界 schema)
```

### `CLASSIFICATION_TOOL_SCHEMA` の形

PoC で 2/2 件合格を確認した形をそのまま採用:

```python
CLASSIFICATION_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category", "topic", "investor_take"],
    "properties": {
        "category": {
            "type": "string",
            "enum": [c.value for c in ValidCategory],  # 12 値
            "description": "...",
        },
        "topic": {
            "type": "string",
            "pattern": "^[a-z0-9]+( [a-z0-9]+)*$",
            "description": "...",
        },
        "investor_take": {
            "type": "string",
            "description": "...",
        },
    },
}
```

`ValidCategory` enum 値だけは `schema.py` 由来で参照する（enum 値の SSoT は `ValidCategory`）。それ以外（pattern、required、property 名）は手書き。

### subset 外制約の扱い

DeepSeek strict mode は以下を **サポートしない**ため、tool schema には書かず、受信後 `ClassificationRawResponse.model_validate_json()` で検証する:

| 制約 | tool schema | Pydantic |
|---|---|---|
| `minLength` / `maxLength` | 書かない | `Field(min_length=1)` 等で残す |
| `minItems` / `maxItems` | 書かない | Pydantic 側で残す |
| `TopicName` の `_TOPIC_MIN_LENGTH=2` / `_TOPIC_MAX_LENGTH=100` | 書かない | `field_validator` 側で残す |

### 整合性検証テスト

`schema_tool.py` と `schema.py` の二重管理ドリフトを構造的に防ぐためのテストを必須で同梱する:

- `CLASSIFICATION_TOOL_SCHEMA["properties"].keys()` == `ClassificationRawResponse.model_fields.keys()`
- `CLASSIFICATION_TOOL_SCHEMA["properties"]["category"]["enum"]` == `[c.value for c in ValidCategory]`
- `CLASSIFICATION_TOOL_SCHEMA["required"]` がすべての property を含む

これにより、`ValidCategory` に値を追加したのに tool schema に反映し忘れた、といったドリフトをテストで検出できる。

### 受信フロー

```
DeepSeek tool_calls.function.arguments  (JSON 文字列)
        ↓
ClassificationRawResponse.model_validate_json(args)  ← Pydantic 再検証
        ↓
classifier 内で Classified | OutOfScope に詰め替え（既存ロジック）
        ↓
ClassificationResponse (既存ドメイン型)
```

`ClassificationRawResponse` は変更しない（既存の Gemini classifier と共有）。

## 実装スコープ (PR 単位)

### PR-A: DeepSeekClassifier 実装 (本命)

- [ ] `backend/app/config.py` に `deepseek_api_key: SecretStr` を追加 + `ai_provider == "deepseek"` 時のバリデーション
- [ ] `.env.example` に `DEEPSEEK_API_KEY=` と `AI_PROVIDER` のコメント更新
- [ ] `backend/app/analysis/classifier/schema_tool.py` (新規) — 手書き flat な `CLASSIFICATION_TOOL_SCHEMA` 定数
- [ ] `backend/app/analysis/classifier/deepseek.py` (新規) — `DeepSeekClassifier(BaseClassifier)`
  - OpenAI SDK + `base_url="https://api.deepseek.com/beta"`
  - Function Calling + `strict: true` + `tool_choice` 強制
  - `extra_body={"thinking": {"type": "disabled"}}`
  - `max_tokens=512`、`summary_ja` 8000 chars truncate
  - `_translate_error` で HTTP 429 / 402 / 5xx をドメインエラーにマップ
  - `MODEL = "deepseek-v4-flash"`、`RPM` / `RPD` は控えめ初期値（公式固定値なしのため動的 429 に依存）
  - 受信後 `ClassificationRawResponse.model_validate_json()` で再検証
- [ ] `backend/app/analysis/errors.py` に `InsufficientBalanceError` を追加
- [ ] `backend/app/analysis/classifier/factory.py` に `provider == "deepseek"` の分岐追加
- [ ] `backend/uv add openai` で OpenAI SDK 依存追加（Ask first 案件）
- [ ] テスト: `tests/test_ai_analyzer.py` に DeepSeekClassifier の単体テスト追加（既存パターン踏襲、`_call_api` を AsyncMock）
- [ ] テスト: factory tests を `deepseek` 経路でも実行
- [ ] **テスト: `CLASSIFICATION_TOOL_SCHEMA` と `ClassificationRawResponse` の整合性を構造検証**
  - property 名一致、enum 値一致、required 完全性

### PR-B: shadow run スクリプト (一時、本命の検証用)

- [ ] `backend/scripts/compare_classifiers.py` — 評価サンプル + 直近データで両 classifier を呼んで比較
- [ ] `discussions/<date>-stage2-shadow-result.md` に結果記録
- [ ] スクリプトは検証完了後の PR で削除
- [ ] 既存 PoC スクリプト (`backend/scripts/poc_deepseek_classifier.py`) も削除

### PR-C: 切替 (運用作業 + 軽い PR)

- 本番 env に `AI_PROVIDER=deepseek` 反映
- worker 再起動
- 24h メトリクス監視
- 異常時は env 戻し + worker 再起動

### PR-D: クリーンアップ (4 週間後)

- [ ] `GeminiClassifier` 削除
- [ ] factory の Gemini 分岐削除
- [ ] 関連テスト削除
- [ ] Stage 1 が Gemini のままなら `google-genai` 依存と Stage 1 用 Gemini クライアントは残す（Classifier 側のコード・テストのみ削除）

## ロールアウト手順

| Phase | アクション | 確認項目 |
|---|---|---|
| 0 | DeepSeek アカウント作成、初回チャージ、`DEEPSEEK_API_KEY` 取得 | 残高 > $20 |
| 1 | PR-A マージ | テスト通過、`AI_PROVIDER=gemini` のままなので本番影響なし |
| 2 | PR-B 実行（ローカル or staging） | 判定基準クリア |
| 3 | レポートをレビュー | 精度・コスト・レイテンシ |
| 4 | 本番 env を `AI_PROVIDER=deepseek` に変更 + worker 再起動 | エラー率、Logfire span |
| 5 | 24h 監視 | 異常時 env 戻し |
| 6 | 1 週間継続観察 | カテゴリ分布、out_of_scope 比率 |
| 7 | 4 週間安定運用後、PR-D（クリーンアップ） | 実装ドリフトなし |

## リスクと緩和

| リスク | 緩和策 |
|---|---|
| beta endpoint の仕様変更 | URL を `config.py` で外出し、Gemini への env 切替で即時 rollback |
| **`schema_tool.py` と `schema.py` の二重管理ドリフト** | **整合性検証テストを必須同梱**（property 名、enum 値、required 完全性）。PR-A の checklist に明記 |
| 残高枯渇による全件停止 | HTTP 402 を `InsufficientBalanceError` で fail fast、env 切替で Gemini に戻す |
| DeepSeek の動的レート制限 | RPM/RPD 公式固定値なし。初期は控えめ値、Logfire 実測で調整。429 backoff は OpenAI SDK の `max_retries` で吸収 |
| `investor_take` 空文字混入 | strict mode は `minLength` 不可のため、受信後 Pydantic で再検証、失敗時 `UnclassifiedError` で task 層リトライ |
| シャドウ期間中の二重コスト | スクリプト型に絞り、評価サンプル件数を絞る (25 + 100 件) |
| Pydantic AI v1 Issue #5193 (deepseek-v4-* で 400) | OpenAI SDK 直接呼び出しで回避 (Pydantic AI を使わない) |
| **DeepSeek strict mode の subset 外制約の挙動変化** | inline schema 形式に閉じ、subset 外制約はすべて Pydantic 側で再検証。spec の挙動を年次でレビュー |

## 参考資料

### DeepSeek 公式
- [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing)
- [JSON Output (JSON Mode)](https://api-docs.deepseek.com/guides/json_mode)
- [Tool Calls (Function Calling + strict mode)](https://api-docs.deepseek.com/guides/tool_calls)
- [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode)
- [Context Caching](https://api-docs.deepseek.com/guides/kv_cache)
- [Rate Limit](https://api-docs.deepseek.com/quick_start/rate_limit)
- [Get User Balance](https://api-docs.deepseek.com/api/get-user-balance)
- [V4 Preview Release](https://api-docs.deepseek.com/news/news260424)

### モデルカード / 比較
- [DeepSeek-V4-Flash (HuggingFace)](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash)
- [DeepSeek V4 Tool Calling Guide (Macaron)](https://macaron.im/blog/deepseek-v4-tool-calling)
- [Artificial Analysis — V4-Flash](https://artificialanalysis.ai/models/deepseek-v4-flash)

### Vector 内部参照
- 既存 Gemini 実装: `backend/app/analysis/classifier/gemini.py`
- ベース: `backend/app/analysis/classifier/base.py`
- factory: `backend/app/analysis/classifier/factory.py`
- 既存 schema: `backend/app/analysis/classifier/schema.py`
- 設定: `backend/app/config.py`
- レート制限: `backend/app/analysis/rate_limiter.py`
- PoC スクリプト: `backend/scripts/poc_deepseek_classifier.py` (PR-B 完了時に削除)
- プロンプト改善計画: `memory: project_prompt_simplification_plan.md`
- Vector エージェント方針: `memory: project_vector_agent_features.md`
