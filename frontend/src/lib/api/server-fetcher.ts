/**
 * Server-side fetcher (Server Components / Route Handlers から backend を直接叩く)。
 *
 * - `INTERNAL_API_URL` をベース URL として結合
 * - `serverFetch`: Better Auth の session から HS256 JWT を Authorization に注入する
 *   per-user 経路。session が取れない build/prerender 時は認証ヘッダなしで投げる
 *   (fail-soft)。
 * - `publicServerFetch`: 認証ヘッダを一切付けない経路。レスポンスが user 非依存な
 *   endpoint 専用。Authorization が user 毎に変わらないため Next.js data cache が
 *   全 user で共有される。
 *
 * features 側はこの 2 関数のみを利用し、URL 解決を意識しない。
 */

import "server-only";

import { requestJson } from "@/lib/api/fetcher";
import {
  buildInternalAuthHeaders,
  INTERNAL_API_URL,
} from "@/lib/api/internal-config";
import { getCurrentSession } from "@/lib/auth/guards";

async function getAuthHeaders(): Promise<Record<string, string>> {
  try {
    const session = await getCurrentSession();
    if (session) {
      return await buildInternalAuthHeaders(session);
    }
  } catch (error) {
    // Session unavailable (typically prerender / build context where
    // `headers()` cannot be resolved). Surface it so genuine failures don't
    // hide behind the same silent catch.
    console.warn(
      "[server-fetcher] session unavailable, sending unauthenticated request",
      error,
    );
  }
  return {};
}

export async function serverFetch<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const authHeaders = await getAuthHeaders();
  return requestJson<T>(`${INTERNAL_API_URL}${path}`, {
    ...options,
    headers: {
      ...authHeaders,
      ...options?.headers,
    },
  });
}

export async function publicServerFetch<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  return requestJson<T>(`${INTERNAL_API_URL}${path}`, options);
}
