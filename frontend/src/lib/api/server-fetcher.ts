/**
 * Server-side fetcher (Server Components / Route Handlers から backend を直接叩く)。
 *
 * - `INTERNAL_API_URL` をベース URL として結合
 * - `serverFetch`: Better Auth の session から HS256 JWT を Authorization に注入する
 *   per-user 経路。`(protected)` 配下 (request-time 確定) からのみ呼ぶ前提で、
 *   build/prerender 文脈では使わない。Better Auth の DB 障害等で session 取得が
 *   throw した場合はそのまま上位の error boundary へ伝搬させ、無音で 401
 *   リクエストが飛ぶ silent corruption を防ぐ。
 * - `publicServerFetch`: 認証ヘッダを一切付けない経路。レスポンスが user 非依存な
 *   endpoint 専用で、`'use cache'` 化が可能。Authorization が user 毎に変わら
 *   ないため Next.js data cache が全 user で共有される。
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
  const session = await getCurrentSession();
  return session ? buildInternalAuthHeaders(session) : {};
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
