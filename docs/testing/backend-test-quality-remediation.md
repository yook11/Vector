# バックエンドテスト品質 監査と修正プラン

> ステータス: Proposed（レビュー待ち） / 調査日: 2026-06-02
> 対象: `backend/tests/`（200 ファイル / 約 43,000 行 / 2,714 tests collected）
> 方法: 21 バケットへ分割した並列分類エージェント → 弱いと判定した各テストを独立エージェントが
> 実際に SUT を改変（mutation）して「壊れた実装でも緑のまま通るか」を実機実行で検証 → 偽陽性を除外。
> 本書は実装着手前の合意用ドキュメント。**まだコードは変更していない。**

## 1. 全体評価

このスイートは **全体として健全度が高い**。21 バケット中、`acquisition` / `collection-sources` /
`collection-toplevel-domain` / `audit` / `insights-trend` / `domain-shared` / `toplevel-infra-misc`
の中核は模範的で、以下のパターンが多くの BC に定着している（付録 C 参照）:

- 実 DB に焼いて読み戻す **stateful integration**（audit / repository / failure_handler 系がほぼ全 BC で一貫）
- 失敗分類の **写像 totality を SSoT から導出**（`test_errors` の matched-pair witness、provider_mapping の網羅性・排他性）
- PII 非漏洩を **独立 detector で negate する anti-test**（redaction / logfire / metrics dump の全文検索）
- reader の **record-replay**（録画した実 payload を公開メソッドに流し parse は本物を動かす）

つまり「テストの書き方を知らない」スイートではない。**確定した弱点は局所的かつ構造的**で、
根本原因は数えるほどのテーマに収束する。over-engineering を足す必要はない。

### 確定 finding サマリ

敵対的 mutation で実機検証して確定したものが **48 件**（全件 `confidence: high`）:

| severity | 件数 | 性質 |
|---|---|---|
| high | 10 | 振る舞いを実際に守れていない / セキュリティ境界欠落 |
| medium | 34 | トートロジー・配線確認止まり・横断ギャップ |
| low | 4 | 局所 |

- 別途 **low severity 48 件** は検証スキップ（個別 severity が低く横断性もない。P2 完了後に棚卸し）。
- **偽陽性 5 件** は検証で否定し除外済み（付録 B）。

最重大は 2 つ:
1. **rate limiter / budget の本質（Lua スクリプト）が全テストで mock により潰され、一行も実行されていない** —
   レート制限と課金 cap という存在意義そのものが未検証。
2. **router の user 分離 / authz 境界が単一ユーザーテストで空虚化** — IDOR・情報漏洩・経済 DoS が緑のまま通る。

---

## 2. テーマ別の問題（根本原因でグルーピング）

### テーマ A — stateful 機構を mock で捏造（Lua/Redis が一行も走らない）【最重要 / high×6】

レート制限・予算 cap の核心ロジックは Lua スクリプト内にあるが、`redis.eval` / `register_script` の
戻り値を固定 mock しているため、スクリプト文字列が Redis に渡らず **本質が完全に未実行**。
mock に教えた値を読み返すトートロジー。

| 位置 | 検証済み mutation（これでも緑） |
|---|---|
| `analysis/rate_limit/test_redis_limiter.py:1`（ファイル全体） | Lua 本体を `return {1,0,'0'}`（常に成功・容量無視）に置換。ZADD/ZCARD/ZREMRANGEBYSCORE/TIME が一切未実行 |
| `analysis/rate_limit/test_redis_limiter.py:29` | `count < max` を `<=`（off-by-one で1件超過許容）に緩めても緑 |
| `analysis/rate_limit/test_redis_limiter.py:44` | `ZREMRANGEBYSCORE` 削除（満杯から永久に回復不能）でも緑。回復は side_effect の順序リストが捏造 |
| `analysis/rate_limit/test_redis_limiter.py:19` | assert 0 件。`acquire()` に `return None` を挿しても緑 |
| `test_maintenance_budget.py:11` | `daily_max - current` → `+ current`（**予算が事実上無制限 = 課金事故**）でも 5/5 緑 |
| `test_maintenance_budget.py:20` | 枯渇分岐 + clamp 削除（上限を一切守らない）でも緑 |
| `test_maintenance_budget.py:48` | clamp 削除・Lua を空文字化しても緑（引数配線のみで本体論理バグを検出不能） |

### テーマ B — API authz / user 分離の欠落（セキュリティ境界が単一ユーザーで空虚）【最重要 / high×3, medium×2】

repository は全 query で `user_id` フィルタを持つのに、テストが常に 1 ユーザーしか登場させない。

| 位置 | 検証済み mutation | 影響 |
|---|---|---|
| `test_routers/test_watchlist.py:104` | `fetch_watched_articles`/`list_ids` から `user_id` WHERE 削除 | **情報漏洩**（他人の watchlist 全件返却） |
| `test_routers/test_watchlist.py:202` | `is_watched`/`unwatch` から `user_id` WHERE 削除 | **IDOR**（他人の entry を削除） |
| `test_routers/test_watchlist.py:162` | `get_current_user`→`get_optional_user` swap | write 経路の未認証ガード退行 |
| `test_routers/test_pipeline.py:13` | pipeline router を admin prefix 外へ移動 | **経済 DoS**（記事取得タスクを大量 dispatch） |
| `test_dependencies_jwt.py:47` | `_user_from_claims` の UUID/UserRole 検証削除 | forged JWT（malformed sub / 不正 role）バイパス |

### テーマ C — wiring-only / over-mocked タスク・CLI【high×1, medium 多数】

SUT のコラボレータ（Service / Handler / AuditRepo）を丸ごと mock し、task / CLI 固有の副作用を見ていない。

- **C-1 hold 負側不変条件**（`stage_hold_reason=None` のとき hold を立てない）が assess/curate/embedding dispatch で未固定。ガード削除しても緑。
- **C-2 dispatch の `isinstance` アサートがクラス階層から恒真**（注入した例外型の再確認）。`is exc` identity に強化すべき。
- **C-3 blocked 経路の `session.commit()` 削除でも緑**（session が MagicMock で監査の永続化が未検証）。briefing dispatcher も同型。
- **C-4 CLI 選択ロジックの空虚化**: `test_re_curate_all.py:186`【high】は `id_from`/`id_to` の比較演算子を入れ替えても緑（`from==to` の degenerate 標本 + 入力非依存 mock）。

### テーマ D — assertion 不在・名前と中身の乖離【medium】

`insights/briefing/application/test_service.py:191`「notifier 例外時も Service が成功する」を謳いながら、
notify が raise しない mock を渡しており **例外時挙動を一度も実行しない**。呼出を削除しても緑。

### テーマ E — cap / 境界が標本不足で恒真

`len(items) <= MAX_ENTRIES` 形のアサートが fixture サイズ ≪ cap のため恒真。
`anthropic` は `[:MAX_ENTRIES]` truncation も lastmod 降順 sort も削除して緑（in-scope 5 件 ≪ 30）。
truncation/sort の所有テストがコードベースに存在しない。

### テーマ F — Alembic vs create_all のドリフト【インフラ根本欠陥 / 要・単独合意】

`conftest.py:202`（`setup_db`）は `Base.metadata.create_all` のみで schema を構築し Alembic を流さない。
そのため `op.execute` で定義された **PL/pgSQL トリガー（in/out scope 相互排他など）が test DB に存在しない**。
プロジェクト旗艦の構造保証を検証するテストを書いても無意味に緑になる（トリガー 2 本を migration から
削除しても全テスト緑）。これは「不変条件は DB 制約で構造的に強制する」という設計方針の中心が
test 側で守れていない根本問題。

### テーマ G — handler の SQLAlchemyError 分岐が公開 API 経由で未到達【横断ギャップ / medium×2+】

`case SQLAlchemyError()` は catch-all と異なり `append_failure`（db_* projection）を呼ぶが、
handler に DB 例外を流すテストが **assessment / embedding / curation の 3 BC で揃って欠落**。
repository の projection テストは handler 分岐への到達を保証しない。1 BC で書いて横展開が効く。

### テーマ H — 局所 missing-coverage / tautology（横断性なし）

NFKC 正規化テストが純 ASCII 入力で空虚（`test_result.py:121,201`）、completer の成功系が isinstance のみで
merge 結果未検証、trend の window 境界 / audit の window_start 値未検証、curation の `ALREADY_*` outcome_code・
drop 経路 injection 信号未固定、など。各 finding の fix をそのまま適用すればよい（付録 A）。

### 基盤の現状（補足）

- `pytest-socket` 等の **network guard なし**、`fakeredis`/`respx` も依存になし。
- `pytest-xdist` なしで **直列前提**（per-test `drop_all` は現状競合しないが将来の並列化を構造的に阻む）。
- `make_internal_jwt`（iss/aud/exp 込み）と `client`/`authed_client`/`admin_client` により、**authz テストの土台は既にある**。

---

## 3. 修正プラン

> 方式決定（2026-06-02 合意済み）: Redis を要するテストは **ephemeral 実 Redis**（`make test-integration` の
> db-test と同型の topology を `@pytest.mark.integration` で立てる）で実体化する。`register_script` /
> `redis.eval` を mock しないことが鍵。fakeredis は Lua eval 対応が version 依存のため採らない。

### P0（high の 8 割をカバー）

| # | テーマ | 内容 | 工数 |
|---|---|---|---|
| 1 | A | ephemeral 実 Redis 基盤を新設し、`SlidingWindowLimiter` と `consume_daily_budget` を behavior で固定 | **L** |
| 2 | B | conftest に第二ユーザー seed + `second_authed_client` 追加 → watchlist user 分離 / IDOR / 未認証401・pipeline authz・jwt malformed を公開 API 契約で固定 | **M** |

**P0-1 で固定する不変条件（実 Redis / mock なし）:**
- rate limiter: `max_requests=N` まで acquire 全成功、`N+1` 回目（block=False）で `RateLimitExceededError`
  （= `count<max` の off-by-one を直接捕捉）/ window 経過後に `ZREMRANGEBYSCORE` 失効 → 再 acquire 成功・`ZCARD` 低下 /
  `asyncio.gather` で `N+1` 本同時 acquire し成功が厳密に `max_requests` 本（atomic check-and-add）
- budget: `daily_max=10`, `requested=6` を 2 回 → granted=6, 4(clamp)、3 回目=0(saturation) / `INCRBY` 累積を `GET` で確認 /
  `EXPIRE` TTL が `0<ttl<=26h` / `asyncio.gather` 同時投入で granted 合計が `daily_max` を絶対超えない
- 既存の mock テストは「Python 側の block 切替・wait 算術の unit」として **命名・コメントで責務限定して残す**（削除しない）

**P0-2 で固定する不変条件（実 DB / mock なし）:**
- watchlist list: admin が article B を watch、user が A を watch → user の GET で `total==1` / `ids==[A]` / B 不在
- watchlist remove: user1 watch → user2 が DELETE → 404 かつ user1 の total 不変（IDOR）
- watchlist POST/DELETE: 未認証 client で 401
- pipeline: 一般ユーザー 403 / 未認証 401（kiq 未呼出も assert すれば dispatch 抑止まで固定）
- dependencies_jwt: `sub='not-a-uuid'` / `role='superuser'` で `get_current_user` が 401（実 DB 不要・unit マーカー）

### P1

| # | テーマ | 内容 | 工数 |
|---|---|---|---|
| 3 | C-4 | CLI id_range を非 degenerate 標本にし `_select_article_ids` の戻り集合で直接 assert + explicit `--limit` + exit3 を end-to-end | M |
| 4 | C-1/C-2 | dispatch の hold 負側（`hold.assert_not_awaited()`）+ `is exc` identity assert（assessment/curation/embedding 横断） | M |
| 5 | G | SQLAlchemyError handler 分岐（1 BC 実装 → 横展開、fake 不要） | S |
| 6 | C-3 | blocked の commit / reject log を実 DB 化（curation tasks + briefing dispatcher） | M |
| 7 | D | briefing notifier 空虚テストに `side_effect=RuntimeError` を与え契約を実行 | S |
| 8 | F | **要・単独合意**: Alembic-upgraded test DB 切替 + 排他トリガーテスト（§4 参照） | L |

### P2

| # | テーマ | 内容 | 工数 |
|---|---|---|---|
| 9 | E | anthropic/ornl の cap+sort を `select` 直叩き（純ロジック）で所有テスト化。`MAX_ENTRIES+ε` 件入力で `len==MAX_ENTRIES` 等値 + 先頭 N 件保持 + lastmod 降順 | S |
| 10 | H | NFKC 全角入力 / completer merge 値 / trend window 値 / completer stale-retry / curation `ALREADY_*`+injection / safe_http http2 観察 / multi_feed read 隔離 | M（合計） |
| 11 | A補 | backfill partial-grant ケース（`0<granted<len(targets)`）追加（budget 基盤完成後） | S |

---

## 4. 着手前に合意が必要な事項

### テーマ F（Alembic-upgraded test DB 切替）

**本筋**: `conftest.py` の `setup_db` を `create_all` から `alembic upgrade head` 構築へ切替え、
トリガー・関数・grant を本番同形にする。これで排他トリガー回帰が検出可能になり、
`test_assessment_repository.py` に cross-table 排他テスト 2 本（in 存在時に save_out が `IntegrityError`、逆も）と
race-lost 勝者不変 assert を追加できる。

**懸念**: これは `create_all` 前提の全 integration テストに影響する重い破壊的変更で、CLAUDE.md の
「DB スキーマ変更 / 設計判断は Ask first」に該当する。局所策（トリガーだけ `create_all` 後に流す）は
DDL 二重管理で drift を生むため非推奨。**この切替は独立タスクとして提案 → 合意を経てから着手する。**
それまで assessment 排他トリガーは「test DB で検証不能」と明示コメントを残し、偽の緑テストを書かない。

### 新規テスト基盤の最小セット

1. **ephemeral 実 Redis fixture**（`@pytest.mark.integration`） — P0-A 必須。Docker/CI に Redis service 追加が要る。
2. **`second_authed_client` / `TEST_USER2_ID` seed**（conftest 拡張） — P0-B 必須。
3. **Alembic-upgraded smoke**（テーマ F のみ・要合意）。
4. network guard は既存 SSRF テストで充足しており新設不要。

---

## 付録 A — 確定 finding 全 48 件

severity → category → 位置 順。各 fix の詳細は監査 raw output を参照。

| # | sev | category | 位置 | 問題(要約) |
|---|---|---|---|---|
| 1 | high | wiring-only | `analysis/curation/cli/test_re_curate_all.py:186` | id 範囲フィルタという実テスト対象を、選ばれた article が本当に target_id かで確認していない。curator mock は対象記事に依らず同じ Signal を返すため、どの 1 件が選ばれても success==1 / awaited_once は緑になる。from==to の標本なので id_from と id_to の役割が入… |
| 2 | high | tautological | `test_maintenance_budget.py:11` | consume_daily_budget 本体は `return int(granted)` するだけで、『below max かどうか』の判定 (daily_max - current の比較、granted の available への clamp、INCRBY 累積) は全て _LUA_CONSUME_BUDGET の Lua 内にある。その Lu… |
| 3 | high | mock-only-should-be-stateful | `test_maintenance_budget.py:20` | 枯渇判定 (`if available <= 0 then return 0`) と枠の累積・EXPIRE はステートフルな本質で Lua 内に実装されているが、eval を 0 固定 mock することでその本質を完全に潰している。複数 worker 同時実行下で厳密に上限を守るという budget.py の存在理由 (課金事故防止) が、どのテストか… |
| 4 | high | missing-coverage | `test_maintenance_budget.py:48` | 本ファイル全体で _LUA_CONSUME_BUDGET の振る舞い (granted の min(requested, available) clamp、複数回呼び出しでの単調増加と daily_max での saturation、INCRBY/EXPIRE のアトミック性) を検証するテストが 1 つも無い。budget.py の core 不変条件… |
| 5 | high | mock-only-should-be-stateful | `analysis/rate_limit/test_redis_limiter.py:44` | 『満杯が時間経過で回復する』という本質 (window_seconds 経過で最古エントリが ZREMRANGEBYSCORE で消え ZCARD が下がる) を Lua スクリプトでなく mock の side_effect 順序で捏造している。回復は test が並べた2要素リストが起こしているだけで、実際の window 回復ロジックは1行も走らな… |
| 6 | high | mock-only-should-be-stateful | `analysis/rate_limit/test_redis_limiter.py:29` | 『満杯』状態を Lua の ZCARD>=max_requests 判定でなく mock の固定戻り値 result[0]==0 で与えているため、容量到達の検出ロジック (count<max_requests 比較) が一切検証されない。block=False 分岐 (Python 側) は見ているが、何をもって満杯とするかの本質は mock 任せ。 |
| 7 | high | missing-coverage | `analysis/rate_limit/test_redis_limiter.py:1` | 枠消費 (ZADD)・window 失効 (ZREMRANGEBYSCORE)・容量判定 (ZCARD < max_requests)・アトミック check-and-add・サーバー TIME を SSoT にする設計 — レートリミッターの存在意義そのもの — が一切実行されない。並行 acquire でのアトミック性も未固定。 |
| 8 | high | missing-coverage | `test_routers/test_watchlist.py:104` | WatchlistRepository.fetch_watched_articles / list_ids は全 query で `WatchlistEntry.user_id == user_id` を WHERE に持つが、conftest は 2 ユーザー(TEST_USER_ID/TEST_ADMIN_ID)を seed しているのに list … |
| 9 | high | missing-coverage | `test_routers/test_watchlist.py:202` | WatchlistService.remove_from_watchlist は is_watched(user_id, article_id) で自分の entry か確認してから unwatch するが、is_watched / unwatch から user_id 条件が抜けても単一ユーザーテストでは検出できない。IDOR(他人の watchlis… |
| 10 | high | missing-coverage | `test_dependencies_jwt.py:47` | dependencies.py の _user_from_claims (95-100行) は sub が UUID parse できない文字列、role が UserRole enum 外の値('superuser' 等) のとき None を返し 401 にする authz 境界ロジックを持つが、claim が『存在するが値が不正』なケースを 1 つ… |
| 11 | medium | missing-coverage | `collection/article_acquisition/reader/test_multi_feed_rss_reader.py:140` | docstring INV-1 は『単一 feed が任意の ExternalFetchError (recoverable/非recoverable 両方) を raise しても他 feed は継続』と謳うが、SUT (multi_feed_rss_reader.py L61) の except は ExternalFetchError と Unre… |
| 12 | medium | tautological | `analysis/assessment/domain/test_result.py:121` | リテラル 'ABC123' の codepoint は実際には 0x41-0x43,0x31-0x33 の純 ASCII であり全角文字を1つも含まない。入力時点で既に半角なので、出力 'ABC123' との一致は normalize_text が何もしなくても成立する。NFKC fold の差で落ちる入力が存在しないため空虚。 |
| 13 | medium | tautological | `analysis/assessment/domain/test_result.py:201` | surface のリテラルは 0x4E,0x56,... の純 ASCII 'NVIDIA' であり全角文字を含まない。入力=期待出力で normalize 無しでも一致するため、正規化の有無を区別できない。 |
| 14 | medium | missing-coverage | `analysis/assessment/test_failure_handler.py:106` | SQLAlchemyError 分岐は catch-all とも Recoverable とも異なる固有契約を持つ: reraise=not last_attempt (catch-all と同じだが)、audit は _audit_unexpected ではなく _audit_failure 経由で project_db_failure が走り、pay… |
| 15 | medium | missing-coverage | `analysis/assessment/test_assessment_audit_repository.py:318` | append_in_scope/out_of_scope は payload に input_text=ready.summary[:_INPUT_TEXT_LIMIT(4096)] と input_text_length=len(ready.summary) を詰める (assessment.py:66-67)。この切詰と長さ記録は監査の情報量・PII… |
| 16 | medium | missing-coverage | `analysis/assessment/test_assessment_repository.py:220` | OOS の対 (test_save_out_of_scope_returns_none_on_race_lost, L141) は『勝者 row が title/summary/investor_take とも不変』まで検証するのに、本テストは assert saved is None のみで勝者 row の不変を一切見ていない。docstring は『… |
| 17 | medium | missing-coverage | `analysis/assessment/test_assessment_repository.py:110` | in_scope_assessments と out_of_scope_assessments は『同一 curation に対して排他 (DB トリガーで強制)』という本プロジェクトの旗艦的構造保証 (trg_in_scope_assessments_no_out_of_scope / trg_out_of_scope_assessments_no_i… |
| 18 | medium | infrastructure-gap | `conftest.py:202` | create_all は SQLAlchemy モデルの table/UniqueConstraint/CheckConstraint しか作らず、Alembic migration の op.execute で定義された PL/pgSQL トリガー (in/out scope 排他、updated_at 等) を一切作らない (models/ に DD… |
| 19 | medium | tautological | `analysis/assessment/test_assess_task_dispatch.py:224` | AssessmentResponseInvalidError は AssessmentRecoverableError のサブクラス。task は exc を素通しするだけで recoverable/terminal の分類も詰め替えもしない。よって isinstance(kwargs['exc'], AssessmentRecoverableError… |
| 20 | medium | missing-coverage | `test_backfill_audit_tasks.py:206` | consume_daily_budget の戻り値が常に 0 か len(targets) のいずれかで、0 < granted < found の partial-grant ケースが一切無い。SUT は `for target in targets[:granted]` で予算分だけスライスして enqueue するが、この境界が固定されていない。d… |
| 21 | medium | tautological | `collection/sources/test_anthropic_adapter.py:100` | anthropic_sitemap.xml の in-scope (/news/) URL は 5 件しかなく MAX_ENTRIES=30 を大幅に下回る。`len(items) <= 30` は cap ロジックの有無に関係なく恒真で、cap 境界 (30 件目で打ち切る) を一切発火させない。AnthropicSource.select の `or… |
| 22 | medium | missing-coverage | `collection/sources/test_anthropic_adapter.py:89` | scope 除外は正しく検証されているが、AnthropicSource.select は `sorted(entries, key=lambda e: e.lastmod or _EPOCH, reverse=True)` で lastmod 降順に並べてから cap する。この『最新 30 件を残す』順序保証 (cap と組で『どの 30 件か』を決… |
| 23 | medium | tautological | `collection/article_completion/test_completer.py:68` | completer の本質は observed (feed) と scraped (HTML) を profile.resolve で merge し AnalyzableArticle に昇格する写像にある。テストは戻り値の型 (isinstance) しか見ず、title/body/published_at が実際に正しく merge されたか・どち… |
| 24 | medium | missing-coverage | `collection/article_completion/test_repository.py:165` | close_claimed の stale ガードは test_state_transitions_ignore_stale_attempt で固定されているが、対称な schedule_retry の `attempt_count == ready.attempt_count` ガードは本ファイルでも他でも直接検証されていない。failure_hand… |
| 25 | medium | over-mocked | `analysis/curation/cli/test_re_curate_all.py:232` | SUT 配下の中核 (execute) と selection を丸ごと差し替えているため、テストは『failed_ids が非空なら exit 3』という run() 内 1 行 (`3 if summary.failed_ids else 0`) と _print_summary の整形のみを検証し、failed_ids が実際にどう発生するか (c… |
| 26 | medium | missing-coverage | `analysis/curation/cli/test_re_curate_all.py:132` | デフォルト 3 件打ち止めは test_run_default_dry_run_processes_3_articles_at_most で見ているが、ユーザが --limit 1 / --limit 10 を渡したときに実際にその件数で絞られる振る舞いがどのテストでも検証されていない。--limit は誤爆防止の安全装置でありユーザ指定上限のコントラクト。 |
| 27 | medium | missing-coverage | `analysis/curation/repository/test_curation_repository.py:151` | SUT は docstring と実装で (1) extracted_at を func.now() で再採番する、(2) 対象 curation が存在しないとき scalar_one() で NoResultFound を送出する、と明記しているが、どちらの不変条件もこのテスト (および repository ディレクトリ全体) で固定されていない。… |
| 28 | medium | missing-coverage | `analysis/curation/test_audit_repository.py:222` | CurationReadyBuildBlockedCode には ALREADY_CURATED / ALREADY_REJECTED_AS_NOISE の 2 つの blocked variant が存在し、これらは『記事現存 → article_id 運搬 → source_id 補填』という CONTENT_TOO_LARGE とは別の outco… |
| 29 | medium | missing-coverage | `analysis/curation/test_audit_repository.py:412` | append_drop_article と _append_failed_event は成功経路と同じく content['injection_markers_present'] が立てば _record_injection_detected を呼ぶ (失敗本文も LLM に露出した境界タグを持ちうる)。だが injection 検知の正本テスト (ma… |
| 30 | medium | missing-coverage | `analysis/curation/test_curate_task_dispatch.py:217` | tasks.py:94-95 で decision.stage_hold_reason が not None のとき set_curation_hold(get_redis(), reason=...) を発火する。keep 経路 (test_keep) だけが hold 発火を assert し、recoverable-exhaustion (Usag… |
| 31 | medium | missing-coverage | `analysis/curation/test_curate_task_dispatch.py:110` | Handler を mock しているため tasks.py 行 94 の `if decision.stage_hold_reason is not None` 分岐 (drop decision は reraise=False/hold=None) のうち hold が呼ばれないことを assert していない。set_curation_hold を… |
| 32 | medium | missing-coverage | `analysis/curation/test_tasks.py:142` | tasks.py:55 で blocked 経路は session.commit() を実行し、:56 で curate_content_rejected を info log する。テストは audit repo の呼び出しと未呼び出し協力者は固定するが、commit の実行 (監査が永続化されること) と reject log の emit を一切 … |
| 33 | medium | wiring-only | `test_shared/test_safe_http.py:239` | 唯一の assert が isinstance(client._transport, _PinnedDnsTransport) で、これは直前の test_uses_pinned_dns_transport と同一かつ _TRANSPORT_KEYS の振分ロジックと無関係に常に真。httpx は transport= 指定時に verify/http1… |
| 34 | medium | missing-coverage | `analysis/embedding/test_embedding_task_dispatch.py:155` | task の Redis hold 配線(embedding.py:92-93 `if decision.stage_hold_reason is not None: await set_embedding_hold(...)`)は、dispatch テスト群では test_terminal_stage_blocked_delegates_to_hand… |
| 35 | medium | missing-coverage | `analysis/embedding/test_failure_handler.py:1` | SQLAlchemyError は catch-all(ValueError)とは別の振る舞いを持つ: append_unexpected_failure ではなく append_failure を呼び(=db_* に projection)、reraise=not last_attempt を返す。catch-all とほぼ同形に見えるが outcom… |
| 36 | medium | assertion-absent | `insights/briefing/application/test_service.py:191` | 主張する不変条件(notifier 例外時の Service 挙動)を全く実行していない。notify が raise する経路に入らないため、Service が notify を try/except で握り潰そうが伝播させようが、このテストは常に緑。名前と中身が乖離した空虚テスト。 |
| 37 | medium | missing-coverage | `insights/briefing/application/test_service.py:62` | service.py 130-147 の並行書込敗北→勝者読戻し→欠落時 RuntimeError という失敗/競合経路に実コードが存在するのにテストが 1 つも無い。故障の見える化を重視する方針下で、競合敗北時に誤って persisted=False/例外握り潰しになっても検出されない。 |
| 38 | medium | missing-coverage | `insights/briefing/llm/test_deepseek_call.py:45` | LLM が submit_weekly_briefing を呼ばなかった(finish_reason=stop 等)という現実的な失敗入力に対する公開契約(non-retryable ConfigurationError)に実コード分岐があるのにテストが無い。誤って空応答を成功扱いすると下流で None 参照クラッシュになりうる。 |
| 39 | medium | mock-only-should-be-stateful | `insights/briefing/tasks/test_tasks_briefing.py:71` | BriefingAuditRepository を丸ごと patch しているため、enqueued audit が実際に PipelineEvent 行として書かれ commit されるか、各 audit が独立 tx か、slug 解決が走るかは一切検証されない。dispatcher の本質(per-category 監査行が DB に残ること)が … |
| 40 | medium | missing-coverage | `insights/trend_discovery/test_service.py:95` | Service 固有のロジックである _jst_midnight_utc による JST 真夜中→UTC 変換 + current_start = current_end - 7d の窓計算が、境界 seed で固定されていない。全 Service テストの seed は window 内部(4/14 等)に置かれており、窓の端(2026-04-13 0… |
| 41 | medium | wiring-only | `insights/trend_discovery/test_tasks_trend_discovery.py:66` | audit helper の必須引数のうち window_start / window_end が一切 assert されていない。append_trend_discovery_run_event_best_effort は window_start: date を必須で取り、監査イベントの窓境界を表す重要フィールドだが、テストは event_type … |
| 42 | medium | assertion-absent | `analysis/rate_limit/test_redis_limiter.py:19` | assert が 0 件で、acquire() が None を返す (正常) 経路を構造的に区別する検証が無い。例外さえ出なければ何が起きても緑。acquired フラグの解釈 (result[0] を int 化し truthy なら return) という本質的分岐を固定していない。 |
| 43 | medium | missing-coverage | `test_routers/test_pipeline.py:13` | test_news_sources.py は test_list_news_sources_forbidden_for_non_admin(403)/test_missing_auth_headers(401)で admin 境界を固定しているが、pipeline 側には同等のテストが無い。pipeline は記事取得タスクを大量 dispatch でき… |
| 44 | medium | missing-coverage | `test_routers/test_watchlist.py:162` | 書き込み系こそ未認証ガードが重要だが、authed_client 前提のテストしか無く、未認証 client での 401 が固定されていない。router は get_current_user 依存で守られているが、その契約をテストが押さえていない。 |
| 45 | low | tautological | `analysis/assessment/test_assess_task_dispatch.py:137` | task (assess_content) には marker 型による分岐が一切なく、except Exception で受けた exc をそのまま handler.handle(exc=exc) へ素通しする。テストが自分で構築・注入した AssessmentCategoryMissingError は定義上 AssessmentTerminalEr… |
| 46 | low | missing-coverage | `analysis/assessment/test_assess_task_dispatch.py:193` | task には `if decision.stage_hold_reason is not None: await set_assessment_hold(...)` という負側ガードがあるが、reason=None の decision を返す本テスト群 (category_missing / recoverable_false / response_… |
| 47 | low | tautological | `collection/sources/test_ornl_adapter.py:112` | ornl_listing.html の in-scope href は 4 件 (dedup 後 3 件) で MAX_ENTRIES=30 を大幅に下回る。`len(items) <= 30` は cap ロジックの有無に依存せず恒真で、ORNLSource.select 内の `if len(result) >= cls.MAX_ENTRIES: b… |
| 48 | low | missing-coverage | `insights/trend_discovery/test_cli_run_trend_discovery.py:117` | run() は window_start = _window_start(window_end) を計算して全 audit 経路(failed/skipped/completed)に window_start を渡しているが、CLI テスト群のどれも audit の window_start kwarg を検証していない。window_start = w… |

---

## 付録 B — 偽陽性 5 件（検証で否定・対応不要）

| 位置 | 主張 | 検証で否定された理由 |
|---|---|---|
| `analysis/assessment/test_assessment_repository.py` | save_in_scope の中核分岐は _get_category_id_by_slug(in_scope.category.value) による slug→category_id 解決とその FK 書込だが、永続行の categor… | 主張は「missing-coverage」だが、その契約(AI が返した category slug → 正しい category_id への解決と FK 書込)は別ファイルで固定されている。  事実関係: 1. 指摘されたテスト test_save_in_scope_persists_snapshot_fields (test_assessment_repository.py:189-217… |
| `collection/article_completion/test_scraper.py` | parse 結果の値を一切検証せず存在 (is not None) のみ確認している。PublishedAt.parse は %Y-%m-%dT%H:%M:%S と %Y-%m-%d を順に試し tzinfo=UTC を付与する純ロジッ… | Read the target test (test_scraper.py:587-593) and the SUT (scraper.py ScrapedContent.try_create -> PublishedAt.parse in value_objects.py:43-53). The test asserts only `published_at is not None`, so… |
| `collection/article_completion/test_repository.py` | 本ファイルは load/claim/sweep/close/schedule の状態遷移を実 Postgres で網羅するが、repository の永続化中核である persist_completed を一度も実行しない。audit_… | 主張は「persist_completed の race-loss 中核（Superseded=DELETE 0 行 / UrlConflict=save None 値化）が全テストスイートで未固定」「2 改変いずれもこのバケットは全て緑」。前半の事実認定は部分的に正しい: (a) test_repository.py は persist_completed を一度も呼ばない（Mutation… |
| `test_shared/test_ssrf_guard.py` | _value は __slots__ で宣言された実スロットなので、immutability を担保している custom __setattr__ を撤去しても __slots__ 自体は既存スロットへの再代入を許す(実測: 宣言済みス… | レビュアーの Python セマンティクス前提は正しいが、結論は逆。前提の正しい部分: __slots__ で宣言済みのスロット(_value)は custom __setattr__ が無ければ普通に再代入できる(`addr._value = '1.1.1.1'` 成功を実測確認)。一方、未宣言属性(addr.foo)は __slots__ 自体が AttributeError を出す。つま… |
| `test_embedding_service.py` | to_embedding_error の 3 バケット目 EMBEDDING_TERMINAL_STAGE_BLOCKED (AIProviderConfigurationError / AIProviderRequestInvalid… | The reviewer's narrow factual observation about test_embedding_service.py is correct: that Service-level file only exercises the recoverable bucket (test_execute_wraps_recoverable_provider_errors) a… |

---

## 付録 C — 保全すべき模範パターン（抜粋）

新規テストはこれらの既存パターンに倣う。劣化させない。

- `analysis/assessment/test_service.py` — commit を patch で raise させ、`pipeline_events` と `in_scope_assessments` 両方のロールバックを実 DB で確認
- `collection/article_acquisition/test_errors.py` — retryable/non-retryable を matched pair (502/403) で witness し片翼の空虚導出を防ぐ
- `collection/sources/test_source_mapping_totality_contract.py` — 全 45 source の collect yield 件数 == named scope を通った entry 件数。期待値を predicate を実適用して入力から導出
- `collection/article_acquisition/reader/test_crossref_reader_contract.py` — record-replay。録画実 payload を公開メソッドに流し、no-drop を `len(entries)==len(_raw_items())` で標本から導出
- `analysis/curation/repository/test_curation_repository.py` — 2 独立セッションを `asyncio.gather` で並行起動し ON CONFLICT race を実 DB で再現
- `test_shared/test_safe_http.py` / `test_ssrf_guard.py` — DNS rebind / マルチホーム private 混在を fake_resolve で再現する security critical テスト
- `test_db_user_isolation.py` — 実 asyncpg 接続で各 role の権限境界を `InsufficientPrivilegeError` として振る舞い検証
- `test_shared/test_redaction.py` / `test_logfire_exceptions.py` — production の置換ロジックと独立した detector で redact 後出力を再走査し secret 残存を否定
