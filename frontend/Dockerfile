# syntax=docker/dockerfile:1.7

# =====================================================================
# Stage 1: deps — package install のみ (依存変更がない場合に build cache が効く)
# =====================================================================
FROM node:20-slim AS deps
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --include=dev

# =====================================================================
# Stage 2: builder — production build (next build で .next/standalone を生成)
# =====================================================================
FROM node:20-slim AS builder
WORKDIR /app

ENV NEXT_TELEMETRY_DISABLED=1
ENV NODE_ENV=production

# auth.ts / internal-config.ts / revalidate route が module load 時に requireEnv
# を呼ぶため、build を完走させるためのダミー値を ARG default に埋め込む。これらは
# bundle に焼かれず、runtime には compose の environment が上書きする。
# placeholder 文字列は git grep で即発見できるよう警告色を付与している。
ARG BETTER_AUTH_SECRET=build-time-placeholder-do-not-use-in-runtime-32bytes
ARG BETTER_AUTH_URL=http://localhost:3000
ARG AUTH_DATABASE_URL=postgresql://build:build@localhost:5432/build
ARG INTERNAL_API_URL=http://backend:8000/api/v1
# 内部 trust 境界の 2 secret (BFF JWT 署名鍵 / revalidate Bearer / red-team C1)。
ARG BFF_JWT_SIGNING_SECRET=build-time-placeholder-do-not-use-in-runtime-bff
ARG REVALIDATE_BEARER_SECRET=build-time-placeholder-do-not-use-in-runtime-rev
ARG REDIS_URL=redis://build-time-placeholder:6379/1
ARG REDIS_URL_RL=redis://build-time-placeholder-rl:6379/0
ENV BETTER_AUTH_SECRET=${BETTER_AUTH_SECRET} \
    BETTER_AUTH_URL=${BETTER_AUTH_URL} \
    AUTH_DATABASE_URL=${AUTH_DATABASE_URL} \
    INTERNAL_API_URL=${INTERNAL_API_URL} \
    BFF_JWT_SIGNING_SECRET=${BFF_JWT_SIGNING_SECRET} \
    REVALIDATE_BEARER_SECRET=${REVALIDATE_BEARER_SECRET} \
    REDIS_URL=${REDIS_URL} \
    REDIS_URL_RL=${REDIS_URL_RL}

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# public/ が無い場合に production stage の COPY が失敗しないよう空ディレクトリを作る
RUN mkdir -p public

RUN npm run build

# =====================================================================
# Stage 3: development — 現行挙動 100% 維持 (next dev、compose の volume mount 前提)
# =====================================================================
FROM node:20-slim AS development
WORKDIR /app

ENV NODE_ENV=development \
    NEXT_TELEMETRY_DISABLED=1

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Better Auth CLI を build-time に /opt/ba-cli に焼き込む。db-init-better-auth
# service が internal network (外部 egress 不可) のまま CLI を起動できるよう、
# runtime での npm install を回避する設計。
#
# Apple Silicon / aarch64 で @better-auth/cli@1.4.x が依存する better-sqlite3
# は linux-arm64 prebuild を持たず (#7761)、node-gyp source build に fallback
# する。frontend dev image は Python を持たないため build が失敗する。
# --ignore-scripts で全 dep の post-install を抑止する。CLI は migrate 時に
# pg/kysely しか load しないため機能影響なし。Better Auth 1.5 stable で
# better-sqlite3 が dependencies から外れ次第 (PR #7771)、--ignore-scripts は
# 撤去予定。
ARG BETTER_AUTH_CLI_VERSION=1.4.22
RUN mkdir -p /opt/ba-cli \
 && cd /opt/ba-cli \
 && npm init -y >/dev/null \
 && npm install --ignore-scripts --no-fund --no-audit \
      "@better-auth/cli@${BETTER_AUTH_CLI_VERSION}" >/dev/null

EXPOSE 3000
# dev stage は host bind mount + .next 書込のため root 維持 (host uid との衝突回避)。
# 本番経路の production stage (Stage 4) は USER nextjs (uid 1001) で non-root 化済で
# 攻撃 surface は閉じている。dev container は localhost のみ expose、external from
# untrusted は到達不可。
# nosemgrep: dockerfile.security.missing-user.missing-user
CMD ["npm", "run", "dev"]

# =====================================================================
# Stage 4: production — standalone server を non-root で起動
# =====================================================================
FROM node:20-slim AS production
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# non-root user (uid 1001 — host dev user の uid 1000 と衝突回避)
RUN groupadd --system --gid 1001 nodejs \
 && useradd  --system --uid 1001 --gid nodejs nextjs

# standalone artifact のみ copy (server.js + 必要 deps の NFT trace + minimal node_modules)
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public

USER nextjs
EXPOSE 3000

# Next.js standalone build には専用 health route なし、root GET 200 で生存確認。
# node:20-slim には curl/wget なし、node 内蔵で完結させる。
# Trivy DS-0026 (HEALTHCHECK 不足) の解消も兼ねる。production stage のみで十分
# (Trivy は最終 stage を評価、dev stage は外部到達なし)。
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
  CMD node -e "require('http').get('http://localhost:3000', (r) => { if (r.statusCode !== 200) process.exit(1); }).on('error', () => process.exit(1));" || exit 1

# next start ではなく node server.js (standalone build の正しい起動経路)。
# `.github/workflows/ci.yml:356-362` のコメントで記録された hydration の罠を回避。
CMD ["node", "server.js"]
