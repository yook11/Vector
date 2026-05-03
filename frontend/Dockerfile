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

# auth.ts / internal-config.ts が module load 時に requireEnv を呼ぶため、
# build を完走させるためのダミー値を ARG default に埋め込む。これらは bundle に
# 焼かれず、runtime には compose の environment が上書きする。
# placeholder 文字列は git grep で即発見できるよう警告色を付与している。
ARG BETTER_AUTH_SECRET=build-time-placeholder-do-not-use-in-runtime-32bytes
ARG BETTER_AUTH_URL=http://localhost:3000
ARG AUTH_DATABASE_URL=postgresql://build:build@localhost:5432/build
ARG INTERNAL_API_URL=http://backend:8000/api/v1
ARG INTERNAL_API_SECRET=build-time-placeholder-do-not-use-in-runtime-32bytes
ARG REDIS_URL=redis://build-time-placeholder:6379/1
ENV BETTER_AUTH_SECRET=${BETTER_AUTH_SECRET} \
    BETTER_AUTH_URL=${BETTER_AUTH_URL} \
    AUTH_DATABASE_URL=${AUTH_DATABASE_URL} \
    INTERNAL_API_URL=${INTERNAL_API_URL} \
    INTERNAL_API_SECRET=${INTERNAL_API_SECRET} \
    REDIS_URL=${REDIS_URL}

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

EXPOSE 3000
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

# next start ではなく node server.js (standalone build の正しい起動経路)。
# `.github/workflows/ci.yml:356-362` のコメントで記録された hydration の罠を回避。
CMD ["node", "server.js"]
