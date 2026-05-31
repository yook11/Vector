#!/usr/bin/env bash
# chain α Better Auth 側補強 + chain ι 解消の手動 PoC。
#
# 前提:
#   - docker compose up -d frontend redis-rl db
#   - frontend は dev (NODE_ENV=development) で起動。production fail-closed は
#     fly deploy 後に別途 Step 4 で観察する (plan の検証手順参照)。
#
# 期待結果:
#   A) same-XFF 8 連発 → 6 件目以降 429 (Better Auth customRules max=5/60s)
#   B) rotation-XFF 8 連発 → dev では bucket 別 (Better Auth が
#      ["fly-client-ip", "x-forwarded-for"] で fallback する設定のため)、
#      production deploy 後は単一 bucket に集約される
#   C) Better Auth ログイン limiter は DB-backed (ADR-007) に移行済み:
#      - redis-rl 内に baRateLimit:* は **存在しない** (auth カウンターは Redis を使わない)
#      - 代わりに auth."rateLimit" テーブルの行が増える
#      - rl:ip:* (proxy.ts 経由) は従来どおり redis-rl に存在する
set -euo pipefail
TARGET="${TARGET:-http://localhost:3000/api/auth/sign-in/email}"
PAYLOAD='{"email":"red-team-test@local","password":"WRONG"}'

echo "=== A: same-XFF (期待: 6 件目以降 429) ==="
for i in $(seq 1 8); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "X-Forwarded-For: 9.9.9.9" \
    -H "Content-Type: application/json" \
    -X POST "$TARGET" -d "$PAYLOAD")
  echo "$i: $CODE"
done

echo ""
echo "=== sleep 65s for window=60 reset ==="
sleep 65

echo ""
echo "=== B: rotation-XFF (dev = bucket 別、production = 単一 bucket) ==="
for i in $(seq 1 8); do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "X-Forwarded-For: 1.2.3.$i" \
    -H "Content-Type: application/json" \
    -X POST "$TARGET" -d "$PAYLOAD")
  echo "$i: $CODE"
done

echo ""
echo "=== C: limiter 保存先の観察 (auth=DB / proxy=redis-rl) ==="
echo "--- baRateLimit:* in redis-rl (期待: 空 = auth は Redis を使わない / ADR-007) ---"
docker compose exec -T redis-rl redis-cli KEYS "baRateLimit:*" | head -10
echo "--- auth.\"rateLimit\" rows in DB (期待: A/B の試行で行が増える) ---"
docker compose exec -T db psql -U vector -d vector \
  -c 'SELECT "key","count","lastRequest" FROM auth."rateLimit" ORDER BY "lastRequest" DESC LIMIT 10;'
echo "--- rl:ip:* in redis-rl (proxy.ts sliding window log 経由 / 従来どおり存在) ---"
docker compose exec -T redis-rl redis-cli KEYS "rl:ip:*" | head -10
