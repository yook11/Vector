# ソース拡張 Roadmap (改訂版)

`phase-plan.md` (2026-04-22) は Phase 1a 実装前に策定された原計画。本ドキュメントは
2026-04-27 の運用実態 + 生フィード再検証 (`verification-2026-04-27.md`) を
踏まえた **改訂後の段階導入計画** を記録する。`phase-plan.md` は履歴として
残し、以後の forward-looking な意思決定は本書を一次情報源とする。

## 起点となる現状 (2026-04-27 時点)

- active 11 source、平日 organic discovery **50-80/day**、週末 **17-20/day**
- フェッチパイプラインは正常 (Redis lag=0、最新 analysis は直近で発火、Gemini
  RPD カウンターは role 別に分離済みで余裕あり)
- 04-21 の `discovered=228` ピークは 04-19/04-20 のパイプライン停止後の
  backfill 効果であり、ベースラインではない (詳細:
  `specs/pipeline-stall-diagnosis-2026-04-26.md`)
- フィード dedup は構造的に正常 (TC / ITmedia AI+ / VB / FB の生フィードと
  DB を 100% 突合確認済み) → discovery 量増加は **「源流の追加」** でしか
  達成できない

## Phase 0: Hacker News 登録漏れの解消

詳細は `phase-0-hn.md`。

スコープ概要:

- `news_sources` への `Hacker News` 行 INSERT (migration)
- `hacker_news.py` の増分ロジックを sliding window 24h に置換
- `hn_fetch_state.py` 削除
- `hn_hits_per_page` 50 → 100
- テスト追加、spec 更新

期待効果: **平日 +20-30/day net new** (URL 重複を差し引き後)。1 PR で完結。
工数最小・ROI 最大。

## Phase 1: Phase 1b 完了 + 英語 Phase 2 主力 (5 ソース)

`phase-plan.md` の Phase 1b は Semiconductor Engineering / CleanTechnica の
2 本構成だったが、**Semiconductor Engineering は Cloudflare 403** で却下
(`verification-2026-04-27.md` 2 節)。代わりに Phase 2 候補から流量・密度の
高いものを前倒しする。

着手順:

| 優先 | ソース | 言語 | 推定 net 増加/day | 補強ドメイン | 着手前提 |
|---:|---|---|---:|---|---|
| 1 | The Register | EN | +10-13 | Networks / Security | リダイレクタ正規化 fetcher 専用化 |
| 2 | Engadget | EN | +6-7 | Mobility / Gadget | base RSS 流用、Yahoo ToS 留意 |
| 3 | CleanTechnica | EN | +10-12 | Energy / EV | base RSS 流用 |
| 4 | Electrek | EN | +8-9 | EV / Energy | base RSS 流用 |
| 5 | SpaceNews | EN | +5-6 | Space | base RSS 流用 |

### 着手分割 (2026-04-29 確定)

5 ソースのうち **The Register のみリダイレクタ正規化が必要**、他 4 本は
`BaseRssFetcher` 継承クラス宣言のみで実装可能 (構造同型 ~7 行)。PR 単位は
2 つに分割する:

- **PR-1 (Phase 1a-rss): 軽量 4 本** (Engadget / CleanTechnica / Electrek /
  SpaceNews) — 1 PR にまとめる。固有挙動なし、`test_rss_base.py` の共通フロー
  テストでカバー、registry smoke テストのみ追加
- **PR-2 (Phase 1b-tregister): The Register** — 単独 PR。`convert_entry`
  上書き + URL 正規化のテストを厚く

着手順は **PR-1 を先行 → deploy / 1-2 サイクル観測 → PR-2**。軽量先行で
リスク低い変更を流通、観測中に PR-2 のテストを設計する。

### The Register リダイレクタ解決方針 (2026-04-29 確定 → PR-2 で実装)

実フィード (`https://www.theregister.com/headlines.atom`) を 3 アイテム実測:

```
<link href="https://go.theregister.com/feed/www.theregister.com/2026/04/28/.../"/>
```

- `/feed/` 以降のパスがそのまま `https://www.theregister.com/...` の実 URL に
  対応
- 全エントリで構造一貫
- `feedburner:origLink` 等の代替フィールドはフィード XML に存在せず

**採用: 案 C (URL パス抽出)**

```python
def convert_entry(self, entry):
    raw_link = entry.get("link", "")
    if "go.theregister.com/feed/" in raw_link:
        real_url = "https://" + raw_link.split("/feed/", 1)[1]
    else:
        real_url = raw_link
    # ArticleCandidate.from_external(...) へ
```

却下:

- **案 A (HEAD)**: ネットワーク往復 +1 / アイテム、タイムアウトリスク
- **案 B (origLink)**: 実フィード XML に存在せず実装不可

実装パターン (各 PR 共通):

1. Alembic migration (news_sources INSERT)
2. fetcher 実装 (RSS 系は既存 `BaseRssFetcher` 流用が原則)
3. `registry.py` に追加
4. テスト追加 (固有挙動あれば独自テスト、なければ smoke のみ)
5. dispatch 1 サイクル後のスモークテスト (`source_fetch_completed` ログ確認)

## Phase 2: 日本語 Phase 2 主力 (3 ソース)

ITmedia / Impress 系。改変禁止 ToS は `README.md:25-29` の運用方針 (事実摘要 +
引用範囲 + 出典明示) で吸収する前提。

着手順:

| 優先 | ソース | 言語 | 推定 net 増加/day | 補強ドメイン | 備考 |
|---:|---|---|---:|---|---|
| 1 | MONOist | JA | +8-10 | Robotics | Robotics ギャップを単独で埋める |
| 2 | EE Times Japan | JA | +4-5 | Semi / Materials | 流量小・密度高 |
| 3 | ITmedia NEWS | JA | +25-35 | AI / Semi / Security 速報 | 流量極大、AI+ との重複可能性は dedup で構造排除 |

ITmedia 系は同じドメイン階層に複数フィードを載せている。条件付き GET
(ETag / Last-Modified) の独立性に問題が出ないか、ITmedia NEWS 投入後
1 日観察してから他 2 本を追加する順序を推奨。

## Phase 3: ニッチ・後段 (要追加判断)

| ソース | 言語 | 状況 | 判断保留の理由 |
|---|---|---|---|
| Schneier on Security | EN | ~1/day だが full content / CC-BY | 「専門ニッチ枠」採用枠の設計が必要 |
| The Record | EN | フィード窓 1 日 5 件 | 30 分ポーリングで取りこぼしが起きないか要確認 |
| ITmedia エンタープライズ | JA | ~6/day, snippet | JPCERT/CC + AI+ との Security 重複品質確認後 |
| INTERNET Watch | JA | ~6-7/day, description 極端に短い | Trafilatura 失敗時の落ちが大きい、本文取得依存度の評価が必要 |
| PC Watch | JA | ~7-8/day, Amazon Deals 混入 | filter 設計コスト |
| Ars Technica | EN | Claude Code WebFetch 拒否 | 実 httpx での疎通検証 + ToS 確認 |

## Phase 4: 別アーキテクチャ要 (後回し)

| ソース | 状況 | 必要な拡張 |
|---|---|---|
| arXiv | abstract + link 形式、3 秒間隔制限、カテゴリ別購読 | 「研究論文ドメイン」を news と分離する設計判断、本文が PDF |
| bioRxiv | 30 件キャップ、カテゴリ分割必要 | 同上 |
| NIH News | PD、Biotech/Medical 補強 | RSS 1 本だが ドメイン的に news/research の境界 |
| NIST | PD、Materials / Security 補強 | 複数トピック feed あり、選別必要 |
| PR TIMES | メディア登録審査次第 | 申請プロセスの確認 |

## 既存 Phase 1a の運用課題 (本 Roadmap の前提)

`specs/pipeline-stall-diagnosis-2026-04-26.md` で指摘された塩漬け問題が一部
未解消:

- `no_extraction` = 616 件 (大半が 04-18/04-19 分)
- `no_embedding` = 271 件 (04-25 のバルク処理時の TEI 失敗分)
- back-fill cron 3 本は実装済み・スケジュール発火中だが
  `BACKFILL_*_ENABLED=False` (デフォルト) のまま無効

本 Roadmap の Phase 0 投入前に、**back-fill kill switch を有効化して塩漬けを
回収しておく** ことを推奨。HN 投入で discovery 量が増える前に、過去分の処理
能力を取り戻しておくため。

## 流量シナリオ

各 Phase 投入後の平日 weekday discovery 想定:

| 状態 | 平日 discovery |
|---|---|
| 現状 (Phase 1a 完了) | 50-80/day |
| Phase 0 後 (HN 追加) | 70-110/day |
| Phase 1 後 (英語 5 本追加) | 110-160/day |
| Phase 2 後 (日本語 3 本追加) | **150-200/day** |

週末は引き続き ~30-50/day 程度に下振れすると見込まれる (英語ソースの
publication が止まるため)。日本語ソースは週末も多少は publish するので
週末 floor は若干上がる。

## 設計上の論点 (各 Phase 着手前に確定したい)

1. **HN extraction 失敗の許容度** (Phase 0): GitHub / X / YouTube / PDF など
   Trafilatura 不適合 URL の rejection 比率を 1 週間観察して、ホスト別
   pre-filter が必要かを再判断する
2. **The Register リダイレクタ解決** (Phase 1): HEAD / origLink / 文字列
   抽出のいずれを採用するか
3. **ITmedia 多フィード並存** (Phase 2): 同階層 ETag/Last-Modified の独立性
   確認、1 本ずつ段階投入
4. **PR 分割粒度**: 1 ソース = 1 PR を原則とする (小粒度・段階リリース)

## 関連ドキュメント

- 戦略全体: [README.md](./README.md)
- 原計画 (2026-04-22): [phase-plan.md](./phase-plan.md)
- スコアリング: [scoring.md](./scoring.md)
- 外部リサーチ検証 (2026-04-22): [verification.md](./verification.md)
- 生フィード再検証 (2026-04-27): [verification-2026-04-27.md](./verification-2026-04-27.md)
- Phase 0 設計: [phase-0-hn.md](./phase-0-hn.md)
- パイプライン停滞診断: [../pipeline-stall-diagnosis-2026-04-26.md](../pipeline-stall-diagnosis-2026-04-26.md)
- パイプライン回復計画: [../pipeline-recovery-plan.md](../pipeline-recovery-plan.md)
