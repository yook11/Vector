# 本番公開前ソース法務レビュー (2026-05-03)

> 既存 20 ソースの ToS / robots.txt 適合性を再評価し、本番公開前の除外判断と
> 代替ソース (Tier 1 / Tier 2) を確定する。
> `source-strategy/README.md` の「法的前提」を上書きする現状認識として位置づける
> (forward-looking な意思決定は本書を一次情報源とする)。

## 1. 背景と本書の位置づけ

`feature/briefing-frontend` で本番公開準備中。Vector は以下を行うため、各ソースの
ToS / robots.txt と整合する必要がある。

- **RSS 自動取得 + 個別記事 HTML 本文の自動取得** (collection.ingestion + extraction)
- **Gemini / DeepSeek-V4 で要約・翻訳・分類・週次 briefing 生成** (Stage 1/2 + briefing) = 翻案
- **ダッシュボードで公衆送信** (insights.briefing + frontend) = 配信

既存 README の法的前提は「30 条の 4 (情報解析目的) + 32 条 (引用)」で全体を整理していたが、
本リサーチで以下が判明したため、より厳格な判断軸を採用する。

- 30 条の 4 は **学習・解析の内部利用** をカバーするが、**AI 翻案結果の公衆送信は射程外**
- 「享受目的併存」の論点 (情報解析が主目的でも享受目的が併存すれば 30 条の 4 の射程外)
- ToS 上の禁止が著作権法より **厳しい** ケースが多い (Vector は ToS 違反でも法的リスクあり)
- 2025-2026 トレンドとして主要メディアの 79% が少なくとも 1 つの AI 訓練 bot を
  robots.txt でブロック (BuzzStream 調査)

## 2. 評価軸

各ソースを以下 3 点で評価し、商用 AI 集約サービスでの利用可否を 3 段階で判定。

1. **robots.txt** — `User-agent: *` の取扱い (Vector / VectorBot 専用ルールは認識されない)
   + AI bot 個別 Disallow / `Content-Signal: ai-train=*` の有無
2. **ToS** — 自動収集禁止 / AI 翻案禁止 / 商用利用禁止 / 集約 DB 作成禁止 / RSS 改変禁止条項
3. **ライセンス** — CC0 / CC BY / CC BY-SA / CC BY-NC* / CC BY-ND / 政府著作物 PD / proprietary

| 判定 | 意味 |
|---|---|
| **GREEN** | 商用 AI 翻案 + 公衆送信 OK (要 attribution) |
| **YELLOW** | 条件付き OK (許諾連絡 / 引用範囲限定 / 表示制約 / crawl-delay 厳守 等) |
| **RED** | 商用 AI 翻案または自動取得が ToS / robots.txt で明示禁止 → 不採用 |

## 3. 既存 20 ソース判定

### RED — 即無効化 (12 件)

| ソース | RED 理由 |
|---|---|
| TechCrunch | ToS で自動 extract/copy/distribute 一般禁止、個人非商用のみ。robots.txt で GPTBot/ClaudeBot/anthropic-ai 全面 Disallow |
| Engadget (Yahoo) | Yahoo ToS §2.4 で scraping/data mining 明文禁止、§2.4(x) で **集約 DB 作成明文禁止 (Vector 直撃)**、§2.15 で RSS 改変禁止 |
| The Register | ToS §2 で個人非商用限定、商用 web は Republishing 有償ライセンス契約必須 (金銭要求明記) |
| IEEE Spectrum | "strictly prohibited without prior written consent" + no modifications + 主要 AI bot 全面 Disallow |
| ITmedia News / AI+ / EE Times Japan / MONOist (4 件) | RSS 利用条件で「文章抜粋転載」「翻案」明文禁止。「インターネット公開は個人非営利でも私的利用に該当しない」「ニュース閲覧アプリでの配信は許諾必須」明記 |
| Microsoft Research | Microsoft.com 全体 ToU が "non-commercial or personal use only" + derivative works 禁止 |
| The Quantum Insider | robots.txt に `Content-Signal: ai-train=no`、ToS で bot/spider/商用/derivative 全面禁止 |
| SpaceNews | robots.txt で **ClaudeBot/Claude-User/Claude-SearchBot を含む主要 AI bot 全面禁止 + python-requests/Go-http-client まで禁止**。Vector スタックそのものが拒否対象 |
| Fierce Biotech (Questex) | B2B 専門メディア、Questex ドメインが WAF で自動アクセス拒否、subscriber redistribute 禁止 |

### YELLOW — 条件付き継続 (6 件)

| ソース | 条件 |
|---|---|
| VentureBeat | robots.txt で ClaudeBot/GPTBot を **明示 Allow**。帰属表示 + 元記事リンク + 要約のみ表示 + 低頻度クロール |
| JPCERT/CC | 引用は明文で自由、再配布は事前連絡。`office@jpcert.or.jp` に許諾メール送付必要 |
| Krebs on Security | `Crawl-Delay: 35` 厳守。AI 翻訳本文公開は内部分析に限定 (現状の Vector は本文非表示のため実質クリア) |
| CleanTechnica | ToS で "personal, noncommercial, home use only" + redistribute 禁止。商用化時に削除 or ライセンス取得 |
| Electrek (9to5) | ToS 不在、reprint 窓口を Wright's Media に集約。フェアユース範囲 (見出し+短い要約+リンクバック) に厳格 |
| Spaceflight Now | ToS 不在。robots.txt 最も緩い。商用化時は事前確認推奨 |

### GREEN (制約なし、2 件)

| ソース | 注意点 |
|---|---|
| Hacker News (Algolia API) | 公式 HN API は **MIT License**。Algolia 経由は scraping 条項対象外 |
| NASA | 連邦著作権 17 U.S.C. §105 で **public domain**。AI 出力に "according to NASA" 直接 attribution を書かない、NASA insignia を AI 生成物に重ねない |

## 4. 今回の調査で確定した追加 RED (新規発見)

代替ソース調査の過程で以下が「採用候補から除外」と判明した。今後の追加候補リストに
入れない。

- **Nature.com** (一般記事) — anthropic-ai/ClaudeBot/GPTBot 等を robots.txt で全面 Disallow
- **The Conversation** — ClaudeBot Disallow + CC BY-ND (派生物禁止) で **二重 NG**
- **Quanta Magazine** — All Rights Reserved (現行 Terms 確定、過去の CC BY 3.0 情報は誤り)
- **MIT Technology Review** — 主要 AI bot 全 Disallow
- **Bloomberg** — 主要 AI bot Disallow + ホワイトリスト式
- **Vox Media / The Verge** — 2025 年 Cohere 提訴、RSL Collective 参加
- **CMU** — personal/non-commercial only 明示
- **University of Tokyo** — 商用要許諾明示、翻案禁止
- **Kyoto University** — All Rights Reserved
- **Stanford News** — CDN/WAF で技術的取得不可 (RSS が 403)
- **JST / SciencePortal** — 営業活動・営利目的禁止明示
- **NIES** — Vector のテック軸との内容適合性低
- **NREL** — RSS 事実上不在
- **NOAA / USPTO** — 連邦 PD だが Vector のカテゴリ充足薄
- **RSL Collective 参加 1500+ 出版社** (Reddit / Yahoo / People Inc. / Quora / O'Reilly / Medium / AP / USA Today / Boston Globe / BuzzFeed / Stack Overflow / Inc. / Fast Company / The Guardian / Slate 等) — 商業契約必須

## 5. Tier 1 GREEN — 即採用候補

各カテゴリの主力ソース。商用 AI 翻案 + 公衆送信 OK、attribution のみ要求。

### 米国連邦政府 (17 U.S.C. §105 PD)

| ソース | RSS endpoint | カテゴリ充足 | 備考 |
|---|---|---|---|
| **NIST** | `https://www.nist.gov/news-events/news/rss.xml` | semiconductor / materials / computing / security / ai (5 Strong) | Vector の中核。ToS 公明 ("public information ... distributed or copied") |
| **NSF** | `https://www.nsf.gov/rss/rss_www_news.xml` | basic research wide | NIST 補完 |
| **NASA** (本体) | `https://www.nasa.gov/feed/`, `/news-release/feed/`, `/technology/feed/`, `/aeronautics/feed/`, `/missions/station/feed/`, `/missions/artemis/feed/` | space (Strong) / mobility / robotics / ai / energy (Some) | API: `api.nasa.gov` (1000 req/h)。AI 出力に NASA insignia / direct attribution 禁止 |
| **ORNL** | `/content/rss-news-feeds-and-podcasts` (HTML scrape backup) | materials / computing / energy (Strong) | "we will not assert its rights against you" 明示。**credit line 必須**: "Courtesy of Oak Ridge National Laboratory, U.S. Department of Energy" |

### EU 公的機関 (CC BY 4.0 ベース)

| ソース | RSS endpoint | カテゴリ充足 | ライセンス |
|---|---|---|---|
| **EU JRC** | `https://joint-research-centre.ec.europa.eu/node/2/rss_en` | ai (AI Act) / energy / bio / security / materials (CRM) / mobility / space | **CC BY 4.0** (Decision 2011/833/EU) |
| **EUR-Lex** | predefined + custom RSS alerts | ai (AI Act) / security (NIS2/CRA/DSA) / bio (EHDS/MDR) / mobility / energy (CBAM) | **CC BY 4.0** |
| **EMA** | `https://www.ema.europa.eu/en/news.xml` ほか 20+ feed | bio (Strong, 新薬承認/SaMD) | 商用 OK 明示 |
| **IAEA** | `/newscenter/news` (Drupal、`/news?` クエリ付きは Disallow 注意) | energy (Strong, SMR/ITER) / security | "freely available" + 商用 OK 明示 |
| **ESA/Hubble** | `https://esahubble.org/news/feed/` | space (Strong) | **CC BY 4.0** 明示。ロゴ非使用、active link 必須 |
| **ESA/Webb** | `https://esawebb.org/news/feed/` | space (Strong) | **CC BY 4.0** 明示 |

### 日本政府 (政府標準利用規約 2.0 / CC BY 4.0 互換)

| ソース | 備考 |
|---|---|
| **文部科学省** | 政府標準利用規約 2.0 / CC BY 4.0 互換明示。商用 AI 翻案 + 公衆送信 + 出典明示で OK |
| **経済産業省** | PDL 1.0 / 政府標準利用規約 2.0 準拠 |
| **総務省** | ODC-By v1.0 / 政府標準利用規約準拠 |

### オープンアクセス学術 (CC BY 4.0)

| ソース | RSS endpoint | カテゴリ充足 | 注意 |
|---|---|---|---|
| **arXiv** | `https://rss.arxiv.org/rss/{cs.AI,cs.LG,cs.CL,cs.NE,cs.RO,cs.CR,quant-ph,cond-mat.mes-hall,physics.app-ph,q-bio,astro-ph,cs.NI}` | ai/security/robotics/computing (Strong) / semi/materials/bio/space/network (Some) | **メタデータ CC0 + per-article ライセンス**。Phase 6 で license-aware 分岐実装 |
| **PLOS** | `https://journals.plos.org/plosone/feed/atom` | bio (Strong) / computing | **CC BY 4.0 統一**、Crawl-delay 30s |
| **eLife** | `https://elifesciences.org/rss/recent.xml` | bio (Strong) | **CC BY 4.0**、Crawl-delay 10s |
| **MDPI** | `https://www.mdpi.com/{journal-issn}/feed` | materials / energy / bio / ai / robotics / security | **CC BY 4.0 統一** |
| **Frontiers** | `https://www.frontiersin.org/journals/{slug}/rss` | ai / bio / materials / energy / robotics / space / mobility | **CC BY 4.0 統一** |
| **engrXiv** | OSF Preprints feed | engineering / robotics / mobility | **デフォルト CC BY 4.0** |

### AI 寛容テックメディア / AI 企業公式

| ソース | 判定根拠 |
|---|---|
| **Cloudflare Blog** | `Content-Signal: ai-train=yes, ai-input=yes, search=yes` 明示。業界最高水準 |
| **Google DeepMind** | robots.txt で主要 AI bot ホワイトリスト Allow |
| **VentureBeat** | robots.txt で ClaudeBot/GPTBot 明示 Allow (既存 YELLOW から GREEN 寄りに昇格) |
| **Anthropic** | robots.txt 完全 Allow。AI モデル発表 primary |
| **OpenAI** | robots.txt 完全 Allow |
| **Meta AI** | robots.txt 完全 Allow。Llama 等の発表 primary |
| **Hugging Face Blog** | robots.txt 完全 Allow。HF ToS で Public repo perpetual license |
| **Cornell Chronicle** | `/about/copyright` で **「news/information media への抜粋・再印刷を事前許諾」明示**。AI category 別 RSS あり。**大学プレスで唯一の GREEN** |

## 6. Tier 2 YELLOW — 条件付き採用

### 米国系
- **JPL**: RSS 不在、HTML scrape のみ。NASA 本体で代替可能なら不要
- **SLAC**: 47 taxonomy RSS 強力、ToS reuse 不明確 → fair-use excerpt + attribution 戦略
- **CISA KEV JSON** (`https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json`): **2025-05-12 に CISA RSS 廃止**。News ではなく structured vulnerability feed として別パイプライン
- **LBNL**: RSS 完璧、UC Regents reuse 申請ベース → fair-use excerpt のみ

### 欧州系
- **CERN** (`https://home.cern/api/news/news/feed.rss`): text license 不明確 → `copyright@cern.ch` に書面照会推奨
- **EUSPA** (`https://www.euspa.europa.eu/rss-press-feed.xml`): RSS 実在、CC 明示なし、強い credit 運用

### 日本系 (要広報連絡)
- **JAXA** (`https://www.jaxa.jp/rss/press_j.rdf` 実機 OK、RSS 廃止情報は誤伝): space (Strong) / robotics
- **IPA** (`https://www.ipa.go.jp/security/rss/alert.rdf`): security (Strong)。JPCERT/CC 補完
- **NICT**: network / security / ai / computing (Strong)
- **NIMS** (`/news/news.xml`): materials (Strong, 日本最強)
- **QST** (`https://www.qst.go.jp/rss/10/list7.xml`): computing (量子) / energy (核融合)
- **NEDO**: ai / energy / semi / materials / robotics
- **AIST**: 11 カテゴリ中 6 で Strong、改変・転載に明示的許可フォーム要求

### プレプリント・OA (license フィルタ要)
- **bioRxiv / medRxiv**: API レスポンスの `license` フィールドで `cc_by` / `cc0` のみ通すフィルタ必須 (CC BY は約 18%)
- **ChemRxiv / TechRxiv**: per-article ライセンス確認、CC BY のみ通過

### 大学プレス (Phase 5 で個別問い合わせ)
- MIT News / UC Berkeley / Caltech / Harvard / Princeton / ETH Zurich / Max Planck

### テックメディア (ToS 確認後 GREEN 化可能)
- Future plc (TechRadar/Tom's Hardware) / Semafor / Axios

## 7. カテゴリ別カバレッジマトリックス (Tier 1 + Tier 2)

| カテゴリ | 主力 (Tier 1) | 補助 (Tier 2) |
|---|---|---|
| **ai** | NIST, NSF, JRC, EUR-Lex (AI Act), arXiv (cs.AI/LG/CL/NE), Frontiers AI, Anthropic/OpenAI/DeepMind/Meta AI/HF Blog, Cornell, VentureBeat, 文科/経産 | AIST, NEDO, NICT, IPA |
| **semiconductor** | NIST, ORNL, arXiv (cond-mat.mes-hall), MDPI Nanomaterials | SLAC, AIST, NIMS, NEDO |
| **materials** | NIST, ORNL, JRC (CRM), MDPI Materials, Frontiers Materials | LBNL, SLAC, ChemRxiv, AIST, NIMS, NEDO |
| **computing** | NIST, ORNL, arXiv (cs.*, quant-ph) | LBNL, SLAC, AIST, QST, NICT |
| **network** | Cloudflare Blog, arXiv (cs.NI) | NICT, EUSPA, Frontiers ICT |
| **security** | NIST, JRC, EUR-Lex (NIS2/CRA), Cloudflare, arXiv (cs.CR), JPCERT | CISA KEV (別 pipeline), IPA, NICT |
| **bio** | EMA, JRC (EHDS), PLOS, eLife, Frontiers, MDPI, arXiv (q-bio), Cornell | bioRxiv (CC BY filter), AIST |
| **energy** | IAEA, JRC, ORNL, MDPI Energies, Frontiers Energy | LBNL, SLAC, NEDO, NIMS, QST |
| **space** | NASA, ESA/Hubble, ESA/Webb, JAXA, arXiv (astro-ph), Frontiers Astronomy | JPL, EUSPA, ESA 本体 (引用のみ), Spaceflight Now (YELLOW 既存) |
| **mobility** | JRC (CBAM/AV), engrXiv, 経産省 | EUSPA (rail/aviation), AIST, Frontiers Future Transportation, Electrek/CleanTechnica (YELLOW 既存) |
| **robotics** | arXiv (cs.RO), Frontiers Robotics & AI, engrXiv, Cornell | TechRxiv (CC BY), AIST, NEDO, MDPI Robotics |

→ **Tier 1 + Tier 2 で 11 カテゴリすべて埋まる**。RED 12 件除外による穴は完全カバー可能。

## 8. 実装計画

### Phase 1 — RED 12 件の無効化 (本番公開ブロッカー)

- alembic migration 1 本: `UPDATE news_sources SET is_active = false WHERE name IN (...)`
- 対象 12 件 (§3 RED 一覧)
- fetcher コード + `FETCHERS` dict 登録は残す (将来許諾取得時に復活可能)
- 検証: `pytest -x` + ローカル `worker-content` 再起動

### Phase 2 — `attribution_label` カラム追加

- `news_sources` に `attribution_label TEXT NULL` を追加
- frontend は `source.attribution_label ?? source.name` を表示するだけ (switch / if 不要)
- 既存 8 件は NULL のまま
- Tier 1 追加 PR で各ソース個別の値を bulk_insert 内で同時投入

**カラム追加の根拠** (撤回した過剰設計案との対比):

| 当初案 | 評価 | 結論 |
|---|---|---|
| `license_type` (PD/CC BY/...) | ランタイムで使わない、ソース選定の判断材料は memory + migration コメントで足りる | **不採用** |
| `tos_status` (approved/permission_required/...) | `is_active` で「使う/使わない」は表現できる、理由はコミットメッセージで足りる | **不採用** |
| `crawl_delay_seconds` | fetcher モジュール定数で完結 (`feedback_source_specific_config_in_module.md`) | **不採用** |
| `attribution_label` | **frontend 表示用**。ESA/Hubble・ESA/Webb・ORNL・文科省など固有 attribution を持つソースが Tier 1 に複数あり、ソース単位で固定文字列。記事ごとに変わるのは arXiv のみ (Phase 6) | **採用** |

### Phase 3 — Tier 1 ソース順次追加 (PR 分割)

構造同型は 1 PR にまとめ、固有ロジックは別 PR (memory: `feedback_phase_pr_split_pattern`)。

| PR | 内容 | 量 |
|---|---|---|
| 3-a | NIST + NSF (米連邦 PD、RSS 標準) | 2 fetcher |
| 3-b | ESA/Hubble + ESA/Webb (CC BY 4.0、構造同型) | 2 fetcher |
| 3-c | PLOS + eLife + Frontiers + MDPI (CC BY 4.0 OA) | 4 fetcher |
| 3-d | Cloudflare Blog + DeepMind + Anthropic + OpenAI + Meta AI + HF Blog (構造同型) | 6 fetcher |
| 3-e | Cornell Chronicle (AI 別 feed、固有) | 1 fetcher |
| 3-f | EU JRC + EMA + IAEA | 3 fetcher |
| 3-g | EUR-Lex custom RSS alerts (AI Act/NIS2/CRA/CBAM) | 1 fetcher (multi-feed) |
| 3-h | 文科省 + 経産省 + 総務省 (政府標準利用規約 2.0) | 3 fetcher |
| 3-i | ORNL + NASA news-release/technology/aeronautics 補強 | 1+ fetcher |

### Phase 5 — Tier 2 許諾取得 (オフライン)

- JPCERT/CC, IPA, NICT, NIMS, QST, NEDO, AIST, JAXA に許諾メール送付
- MIT News / UC Berkeley / Caltech / Harvard / Princeton / ETH Zurich / Max Planck の reprint policy 個別問い合わせ
- 取得後に fetcher 追加 (Phase 3 と同じ要領)

### Phase 6 — arXiv 専用

- `news_articles` に `license` カラム追加
- arXiv RSS の `<dc:rights>` パース
- briefing 生成で `cc_by`/`cc0`/`cc_by_sa` のみ翻案、それ以外はメタデータ + リンクのみ
- 本文 PDF は取得しない (abstract で十分)

## 9. 表示要件の整理 (タイプ別)

各ソース固有の要件を実装場所別に分類。

| タイプ | 内容 | 実装場所 | 数 |
|---|---|---|---|
| 1 (シンプル) | "Source: <name>" で足りる | 既存表示 | 大多数 |
| 2 (CC BY 表記) | "ESA/Hubble · CC BY 4.0" 等 | `attribution_label` カラム (Phase 2) | 5+ |
| 3 (日本政府指定) | "出典：文部科学省ホームページ" | `attribution_label` カラム | 3 |
| 4 (NASA 出力制約) | AI 出力に "according to NASA" を書かない | briefing prompt | 1 (理論上、優先度低) |
| 5 (本文出さない) | Krebs/Electrek/CleanTechnica の本文翻訳を briefing に出さない | **既に解決済み** (現状の Vector は本文を LLM にも frontend にも出していない) | 4 |

**現状実装の確認結果** (2026-05-03):

- `backend/app/insights/briefing/domain/article.py`: ArticleInput は `id / title_ja / summary_ja` のみ。原文本文を LLM に流していない (コメントに明記)
- `backend/app/insights/briefing/schemas/briefing.py`: briefing 出力は `headline + stories[].title + stories[].analysis` のみ。本文ゼロ
- `frontend/src/features/news/components/NewsDetail.tsx`: 個別記事ページは `translatedTitle / original.title / source.name / summary / investorTake / "Read Original Article" リンク` のみ。原文本文の表示なし

→ タイプ 4/5 は構造的にほぼ解決済み。Phase 1/2/3 のブロッカーにはならない。

## 10. 撤退基準 (継続監視)

- robots.txt に Vector 固有 UA / `User-agent: *` Disallow 追加 → 即停止
- ToS に AI 翻案・自動収集禁止が新規追加 → 即停止
- cease and desist letter 受領 → 即停止 + 過去配信記事の取り下げ
- robots.txt + ToS の **四半期再検査サイクル** (2026-08, 2026-11, 2027-02, ...)

## 11. 判断保留項目

| 項目 | 選択肢 |
|---|---|
| AI 要約 `summary_ja` の引用範囲が Krebs/Electrek/CleanTechnica の YELLOW 条件 (フェアユース) を満たすか確認するか | A. Stage 1 プロンプトと実出力サンプルを確認 / B. 確認しない (現状放置) |
| briefing `analysis` 内の direct attribution (NASA の "according to NASA" 抑制) | A. プロンプトに source 名抑制ルール追加 / B. 優先度低として放置 |
| Phase 1 と Phase 2 を同 migration にまとめるか | A. 1 migration / B. 2 migration |
| Phase 3 の着手順序 | a→b→c の順 / 価値の高い d (AI 企業 blog) を先行 |
| User-Agent を `Vector/2.0 (+https://<domain>; bot@<domain>)` 形式に変更するか (各サイトが個別制御できるように) | A. Phase 4 で実施 / B. 不要 |

## 関連

- `source-strategy/README.md` — 全体ハブ (本書で「法的前提」を上書き)
- `source-strategy/roadmap.md` — 既存ソース拡張計画 (本書はその後の法的レビュー)
- `category-taxonomy.md` — 11 カテゴリ定義
- `collection-source-rss-research.md` — RSS feed 実測リサーチ (2026-04-30)
- `collection-source-data-inventory.md` — Tier 1/2/3 データ分類 (2026-04-30)
