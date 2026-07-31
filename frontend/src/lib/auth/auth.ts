import "server-only";

import { betterAuth } from "better-auth";
import { v7 as uuidv7 } from "uuid";
import { authRateLimit, passwordPolicy } from "@/lib/auth/auth-config";
import { authPool } from "@/lib/auth/auth-db";
import { requireEnv } from "@/lib/env";
import { CLIENT_IP_HEADER } from "@/lib/proxy/identifier";

export const auth = betterAuth({
  database: authPool,
  basePath: "/api/auth",
  emailAndPassword: {
    enabled: true,
    disableSignUp: true,
    minPasswordLength: passwordPolicy.minLength,
    maxPasswordLength: passwordPolicy.maxLength,
  },
  user: {
    additionalFields: {
      role: {
        type: "string",
        defaultValue: "user",
        input: false,
      },
    },
  },
  session: {
    // cookieCache は無効化: 有効にすると最大 maxAge 秒間 stale な role / session が
    // 認可判定に使われ、admin 降格や session revoke の即時反映が効かなくなる。
    cookieCache: { enabled: false },
  },
  trustedOrigins: [requireEnv("BETTER_AUTH_URL")],
  // Better Auth 内蔵 rate-limit は `/api/auth/*` router 専用。
  // proxy.ts の全域 rate-limit と分担し、credential stuffing 系の window を伸ばす。
  // 保存先は DB (auth schema)。設定は auth-config.ts に集約し migration と共有する。
  // customRules の path は normalizePathname で basePath strip 済 (`/api/auth` は含めない)。
  rateLimit: authRateLimit,
  advanced: {
    // proxy.ts が信頼モード (CLIENT_IP_TRUST) で解決し必ず上書きする内部ヘッダ
    // のみを読む。生ヘッダを直接読まないのは、解決を identifier.ts に一元化するためと、
    // Better Auth は trustedProxies 無しだと複数値ヘッダを解決できないため。
    // 欠如時は Better Auth が共有 per-path bucket へ fallback し、proxy.ts 側の
    // missing_ip 信号で可視化される (specs/client-ip-trust-mode.md)。
    ipAddress: {
      ipAddressHeaders: [CLIENT_IP_HEADER],
      disableIpTracking: false,
    },
    database: {
      // UUIDv7 (時刻順) を採用。crypto.randomUUID() の v4 はランダム順で
      // B-tree index が断片化するため、書込負荷下の性能を見据えて v7 を選択。
      generateId: () => uuidv7(),
    },
  },
});
