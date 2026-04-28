/**
 * Server-side fetcher (Server Components / Route Handlers から backend を直接叩く)。
 *
 * - `INTERNAL_API_URL` をベース URL として結合
 * - Better Auth の session から HS256 JWT (`Authorization: Bearer <jwt>`) を注入
 * - build 時など session が取れないコンテキストでは認証ヘッダなしで投げる (fail-soft)
 *
 * features 側はこの関数だけを利用し、認証や URL 解決を意識しない。
 */

import { headers } from "next/headers";
import { requestJson } from "@/lib/api/fetcher";
import {
  buildInternalAuthHeaders,
  INTERNAL_API_URL,
} from "@/lib/api/internal-config";
import { auth } from "@/lib/auth/auth";

async function getAuthHeaders(): Promise<Record<string, string>> {
  try {
    const session = await auth.api.getSession({
      headers: await headers(),
    });
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
