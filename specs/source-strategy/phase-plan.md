# Phase Plan: ソース段階的拡張

## Phase 1a: 即時実装 (既存5本 + 新規8本 = 13本)

### 既存維持 (5本)

| ソース | タイプ | フィードURL | 主要分野 | 備考 |
|--------|--------|-----------|---------|------|
| TechCrunch | RSS | `techcrunch.com/feed/` | AI/Tech全般 | スニペット、Trafilatura必須 |
| Hacker News | API | `hn.algolia.com/api/v1/search_by_date` | 全分野 (シグナル層) | Phase 0 (2026-04-29) で sliding window 設計に修正、初投入 (詳細: `phase-0-hn.md`) |
| The Quantum Insider | RSS | `thequantuminsider.com/feed/` | Next-gen Computing | `content:encoded` 対応済み |
| FierceBiotech | RSS | `fiercebiotech.com/rss/xml` | Biotech | スニペット |
| ITmedia | RSS | ※下記「ITmedia分割」参照 | Tech日本語 | 分割対応 |

### ITmedia 分割

現在の `itmedia_all.xml` (全カテゴリ) を無効化し、**ITmedia AI+ (`aiplus.xml`) 単独に切り替え**。

理由:
- `itmedia_all.xml` は全カテゴリ混在で、10ドメイン外の記事 (ビジネス、モバイル等) が大量に入りノイズになる
- AI+ に絞ることでドメイン精度が上がる
- Phase 2 で他トピックフィード (EE Times Japan, MONOist, スマートジャパン等) を順次追加

### 新規追加 (8本)

| # | ソース | スコア | 言語 | 主要分野 | フィードURL | コンテンツ |
|---|--------|-------|------|---------|-----------|----------|
| 1 | IEEE Spectrum | 91 (S) | EN | 全10分野 | `spectrum.ieee.org/feeds/feed.rss` | Full (~15,800字) |
| 2 | NASA | 91 (S) | EN | Space/Material | `nasa.gov/news-release/feed/` | Full (PD) |
| 3 | Microsoft Research | 90 (S) | EN | AI/Computing | `microsoft.com/en-us/research/feed/` | Full (~47,000字) |
| 4 | Krebs on Security | 84 (A) | EN | Security | `krebsonsecurity.com/feed/` | Full |
| 5 | VentureBeat | 82 (A) | EN | AI | `venturebeat.com/feed/` | Full (~8,500字) |
| 6 | Spaceflight Now | 81 (A) | EN | Space | `spaceflightnow.com/feed/` | Full (~3,500字) |
| 7 | ITmedia AI+ | 74 (B) | JA | AI | `rss.itmedia.co.jp/rss/2.0/aiplus.xml` | Snippet |
| 8 | JPCERT/CC | 79 (A) | JA | Security | `jpcert.or.jp/rss/jpcert.rdf` | Summary (RDF) |

### 無効化 (4本)

| ソース | 理由 |
|--------|------|
| Yahoo Finance | 対象10分野外、スニペット品質低 |
| Cointelegraph | 暗号通貨は10ドメイン外 |
| Alpha Vantage | 金融ニュース、日次25リクエスト制限 |
| BioPharma Dive | FierceBiotechで代替可、専門性過度 |

## Phase 1b: 疎通確認後 (新規2本)

| ソース | スコア | 言語 | 主要分野 | フィードURL | 確認事項 |
|--------|-------|------|---------|-----------|---------|
| Semiconductor Engineering | 67 (B) | EN | Semi | `semiengineering.com/feed/` | Cloudflare 403 の疎通確認 |
| CleanTechnica | 66 (B) | EN | Energy | `cleantechnica.com/feed/` | Trafilatura必須、スニペット確認済み |

## Phase 2: 安定後の拡張

### 日本語拡張 (ITmedia/Impress系)

| ソース | フィードURL | 分野 | 備考 |
|--------|-----------|------|------|
| ITmedia NEWS | `rss.itmedia.co.jp/rss/2.0/news_bursts.xml` | AI/Semi/Security | 改変禁止条項あり、引用範囲運用 |
| ITmedia エンタープライズ | `rss.itmedia.co.jp/rss/2.0/enterprise.xml` | Security/Enterprise | |
| MONOist | `rss.itmedia.co.jp/rss/2.0/monoist.xml` | Robotics/Manufacturing | |
| EE Times Japan | `rss.itmedia.co.jp/rss/2.0/eetimes.xml` | Semi | |
| スマートジャパン | `rss.itmedia.co.jp/rss/2.0/smartjapan.xml` | Energy | |
| PC Watch | `pc.watch.impress.co.jp/data/rss/1.0/pcw/feed.rdf` | Semi/Computing | URL訂正済み (旧rssad.jpは死亡) |
| INTERNET Watch | `internet.watch.impress.co.jp/data/rss/1.0/iw/feed.rdf` | Networks/Security | URL訂正済み |

### 英語拡張

| ソース | フィードURL | 分野 | 備考 |
|--------|-----------|------|------|
| The Register | `theregister.com/headlines.atom` | Semi/Security/Enterprise | Atom、スニペット |
| SpaceNews | `spacenews.com/feed/` | Space | 業界紙 |
| ESA | `esa.int/rssfeed/Our_Activities` | Space | CC-BY Attribution 4.0 |
| Schneier on Security | `schneier.com/feed/atom/` | Security/Crypto | Full (Atom) |
| The Record | `therecord.media/feed` | Security | スニペット |
| Engadget | `engadget.com/rss.xml` | Mobility/Gadget | Full、Yahoo ToS留意 |

### 研究層 (アーキテクチャ拡張込み)

ニュースソースとは別のフェッチャーアーキテクチャが必要。

| ソース | フィードURL | 分野 | 備考 |
|--------|-----------|------|------|
| arXiv | `rss.arxiv.org/rss/{category}` | AI/Security/Material/Biotech | Atom、カテゴリ別購読、3秒間隔制限 |
| bioRxiv | `connect.biorxiv.org/biorxiv_xml.php?subject={cat}` | Biotech | 30件キャップ、カテゴリ分割必要 |
| NIH News | `nih.gov/news-releases/feed.xml` | Biotech/Medical | PD |
| NIST | `nist.gov/news-events/news/rss.xml` | Security/Materials | PD、複数トピックfeedあり |

### 審査待ち

| ソース | 状態 | 備考 |
|--------|------|------|
| PR TIMES | メディア登録審査次第 | 通過すればB-Tier相当 (全分野カバー、日量500+) |

## ドメインカバレッジ (Phase 1a + 1b 完了時)

| ドメイン | カバーソース | 充足度 |
|---------|------------|-------|
| AI | TechCrunch, Microsoft Research, VentureBeat, ITmedia AI+, IEEE Spectrum | 充実 |
| Robotics/Mobility | TechCrunch, IEEE Spectrum | 弱め → Phase 2 で MONOist |
| Semiconductors | IEEE Spectrum, Semiconductor Engineering, TechCrunch | 充実 |
| Next-gen Computing | The Quantum Insider, IEEE Spectrum, Microsoft Research | 充実 |
| Next-gen Networks | IEEE Spectrum | 弱い → Phase 2 で The Register / INTERNET Watch |
| Security | Krebs, JPCERT/CC, IEEE Spectrum | 充実 |
| Space | NASA, Spaceflight Now, IEEE Spectrum | 充実 |
| Biotech | FierceBiotech, IEEE Spectrum | 充実 |
| New Materials | IEEE Spectrum | 弱い → Phase 2 で MONOist / NIST |
| Energy | CleanTechnica, IEEE Spectrum | 充実 |

ギャップ3分野 (Robotics, Networks, Materials) は Phase 2 で解消予定。全分野で最低1ソースはカバー済み。

## 未決定事項

- [ ] Spaceflight Now vs The Register の Phase 1a 優先度 (現状: Spaceflight Now を Phase 1a に採用)
- [ ] Phase 1a 新規8本の実装順序
- [ ] Phase 1b の Cloudflare 疎通テスト実施
- [ ] PR TIMES メディア登録の申請プロセス確認
