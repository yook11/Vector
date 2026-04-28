# 候補ソース実フィード再検証 (2026-04-27)

`phase-plan.md` (2026-04-22) の Phase 1b / Phase 2 候補について、Phase 1a
実装後の運用が始まった 2026-04-27 時点で **WebFetch による実取得** を行い、
採用判断に必要なデータを再収集した結果。

`verification.md` (2026-04-22) が外部リサーチ文書の主張を検証する文書だったのに
対し、本書は **生フィードの実測** に焦点を絞る。

---

## 1. Phase 1a 実装後の運用実態 (2026-04-27 計測)

| ソース | DB 累計 | 直近 discover | 平日 organic 流量推定 |
|---|---:|---|---|
| TechCrunch | 170 | 2026-04-27 | ~20/day (US 平日のみ) |
| ITmedia AI+ | 95 | 2026-04-27 | ~10-15/day |
| FierceBiotech | 73 | 2026-04-25 | ~5-7/day |
| The Quantum Insider | 55 | 2026-04-27 | ~3/day |
| NASA | 43 | 2026-04-24 | ~5-10/day |
| IEEE Spectrum | 37 | 2026-04-24 | ~1-2/day |
| VentureBeat | 32 | 2026-04-26 | ~3-5/day |
| JPCERT/CC | 30 | 2026-04-27 | 不定 (週次中心) |
| Spaceflight Now | 13 | 2026-04-27 | ~1/day |
| Krebs on Security | 11 | 2026-04-21 | 週 2-3 本 |
| Microsoft Research | 11 | 2026-04-22 | 週 1-2 本 |

平日 organic 合計 ≒ **50-80/day**。週末 ~17-20/day。

`phase-plan.md` の「Phase 1a: 既存5本 + 新規8本 = 13本」に対し、実装は
**11 active**。差分の発生源:

- **Hacker News (既存維持枠)**: registry 登録済みだが `news_sources` テーブルへの
  INSERT migration が無く、**実体として未登録**。`phase-plan.md:10` の
  「既存フェッチャー稼働中」という記述は実態と乖離。詳細は `phase-0-hn.md` 参照。
- **Phase 1b の Semiconductor Engineering / CleanTechnica**: 疎通確認待ちで未実装。

## 2. Phase 1b 候補

| # | ソース | Status | Items | Most recent (UTC) | Oldest (UTC) | 本文密度 | 日量推定 | Issues |
|---|---|---|---:|---|---|---|---:|---|
| 1 | Semiconductor Engineering | **403** | – | – | – | – | – | Cloudflare/anti-bot 403。`?type=rss2` でも 403。UA 偽装/スクレイパ対応が前提 |
| 2 | CleanTechnica | 200 | 45 | 2026-04-27 03:50 | 2026-04-23 03:37 | partial (~260-520 字) | ~11/day | 異常なし |

判定:

- **CleanTechnica**: 採用 OK
- **Semiconductor Engineering**: 却下。Semi 強化は Phase 2 候補の
  EE Times Japan + 既存 IEEE Spectrum で代替する

## 3. Phase 2 候補 (英語 10 本)

| # | ソース | Status | Items | Most recent (UTC) | Oldest (UTC) | 本文密度 | 日量推定 | Issues |
|---|---|---|---:|---|---|---|---:|---|
| 1 | The Register | 200 | 50 | 2026-04-27 09:35 | 2026-04-23 10:45 | partial (~350-420 字) | ~12-13/day | `<link>` が `go.theregister.com/feed/...` 経由のリダイレクタ。実 URL 解決ロジックが必要 |
| 2 | SpaceNews | 200 | 24 | 2026-04-26 09:56 | 2026-04-22 08:10 | partial (~310-400 字) | ~5-6/day | 異常なし |
| 3 | ESA | 200 | 9-10 | 2026-04-24 13:15 | 2026-01-21 12:54 | snippet〜partial (85-420 字) | ~0.1/day | 投稿頻度が極端に低い (3 か月で 9 件)、画像のみアイテム混入 |
| 4 | Schneier on Security | 200 | 10 | 2026-04-24 21:03 | 2026-04-15 10:47 | full (~550-620 字) | ~1/day | CC 系で再配布許可明確、専門ニッチ枠 |
| 5 | The Record | 200 | 5 | 2026-04-24 19:15 | 2026-04-24 13:45 | snippet (~170-220 字) | ~5/day | フィード窓が極端に狭い (1 日のみ)、`content:encoded` 無し |
| 6 | Engadget | 200 | 10 | 2026-04-26 20:03 | 2026-04-25 12:00 | **full (~1,680-1,920 字)** | ~6-7/day | Yahoo 配下、本文密度高 |
| 7 | Ars Technica | **BLOCKED** | – | – | – | – | – | Claude Code 側 WebFetch で fetch 拒否、実運用 httpx での再検証が必要 |
| 8 | Electrek | 200 | 50 | 2026-04-27 03:45 | 2026-04-21 22:55 | partial (~380-650 字) | ~8-9/day | 異常なし |

判定:

- **採用 OK**: The Register (リダイレクタ正規化アダプタ要), CleanTechnica,
  Engadget, Electrek, SpaceNews
- **保留**: Schneier (~1/day だが質高、専門枠として後段)、The Record
  (フィード窓が狭く取りこぼし懸念、ポーリング頻度の妥当性検証が必要)
- **却下**: ESA (~0.1/day、Space は SpaceNews + 既存 NASA/Spaceflight Now で十分)
- **要追加検証**: Ars Technica (実 httpx での疎通とToS 確認)、Semiconductor
  Engineering (Cloudflare 突破策が無ければ恒久却下)

## 4. Phase 2 候補 (日本語 7 本)

| # | ソース | Status | Items | Most recent (JST) | Oldest (JST) | 本文密度 | 日量推定 | Issues |
|---|---|---|---:|---|---|---|---:|---|
| 1 | ITmedia NEWS | 200 | 50 | 2026-04-27 18:55 | 2026-04-26 07:14 | snippet (~150-280 字) | ~35-40/day | 改変禁止 ToS (ITmedia 共通) |
| 2 | ITmedia エンタープライズ | 200 | 50 | 2026-04-27 13:45 | 2026-04-19 07:00 | snippet (~145-185 字) | ~6/day | 改変禁止 ToS、Security 系で JPCERT/CC と重複可能性 |
| 3 | MONOist | 200 | 30 | 2026-04-27 18:55 | 2026-04-24 07:00 | snippet (~195-230 字) | ~8-10/day | 改変禁止 ToS、**Robotics ギャップを単独で埋められる** |
| 4 | EE Times Japan | 200 | 20 | 2026-04-27 15:30 | 2026-04-23 09:30 | snippet (~145-220 字) | ~4-5/day | 改変禁止 ToS、Semi/Materials 直接補強 |
| 5 | スマートジャパン | 200 | 20 | 2026-04-27 07:30 | 2026-03-31 07:00 | snippet (~140-280 字) | ~0.7/day | 更新頻度低、Energy は CleanTechnica/Electrek が圧倒的 |
| 6 | PC Watch | 200 | 20 | 2026-04-27 18:31 | 2026-04-25 11:33 | snippet (~110-155 字) | ~7-8/day | Impress 改変禁止、Amazon Deals 等のノイズ混入 |
| 7 | INTERNET Watch | 200 | 20 | 2026-04-27 18:00 | 2026-04-24 13:30 | snippet (~45-110 字) | ~6-7/day | description 極端に短く Trafilatura 本文取得依存度大 |

改変禁止 ToS は全ソース共通の制約として「事実摘要 + 引用範囲 + 出典明示」で
吸収する前提 (`README.md:25-29` の運用方針)。

判定:

- **採用 OK**: MONOist (Robotics ギャップ単独充填), EE Times Japan
  (Semi/Materials 補強), ITmedia NEWS (流量 35-40/day)
- **保留**: ITmedia エンタープライズ (Security 重複懸念で品質確認後)、PC Watch
  (Amazon Deals ノイズの filter 設計コスト)、INTERNET Watch (description
  情報密度が低く、Trafilatura 失敗時の落ちが大きい)
- **却下**: スマートジャパン (~0.7/day で投資対効果低)

## 5. Hacker News 実測

`https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=50&numericFilters=points%3E20`
を WebFetch:

- HITS_RETURNED: 50
- OLDEST_HIT_TIME: 2026-04-26 12:47:47 UTC
- NEWEST_HIT_TIME: 2026-04-27 08:11:16 UTC
- WITH_URL_COUNT: 50/50

19.5h で 50 hits = 24h で **~60 stories**。**hitsPerPage=50 は 24h ウィンドウで
オーバーフローする可能性**。100 への引き上げを推奨 (詳細は `phase-0-hn.md`)。

## 6. 採用判定サマリ

採用予定 (Phase 0 + 1 + 2):

| ソース | 言語 | 推定 net 増加/day | 補強ドメイン | 着手 Phase |
|---|---|---:|---|---|
| Hacker News | EN | +20-30 | 全分野 (シグナル) | Phase 0 |
| The Register | EN | +10-13 | Networks/Security | Phase 1 |
| CleanTechnica | EN | +10-12 | Energy/EV | Phase 1 |
| Engadget | EN | +6-7 | Mobility/Gadget | Phase 1 |
| Electrek | EN | +8-9 | EV/Energy | Phase 1 |
| SpaceNews | EN | +5-6 | Space | Phase 1 |
| MONOist | JA | +8-10 | Robotics | Phase 2 |
| EE Times Japan | JA | +4-5 | Semi/Materials | Phase 2 |
| ITmedia NEWS | JA | +25-35 | AI/Semi/Security 速報 | Phase 2 |

合計推定: **+97-127/day**。ベースライン 50-80/day → **150-200/day weekday**
が現実的射程。

保留 (要追加判断): Schneier on Security, The Record, ITmedia エンタープライズ,
PC Watch, INTERNET Watch, Ars Technica。

却下 (恒久): ESA, スマートジャパン, Semiconductor Engineering (Cloudflare
突破策が見つからない限り)。
