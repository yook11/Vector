/**
 * hey-api singleton client への interceptor 登録 (side-effect モジュール)。
 *
 * Vector が必要とする 2 つの cross-cutting concern を attach する:
 * 1. auth: Better Auth session があれば HS256 JWT を Authorization header に注入
 * 2. error 正規化: backend が返す 4xx/5xx の生 JSON / text を ApiError(status, detail)
 *    に整形して throw (`throwOnError` フラグの設定値に依存させない)
 *
 * 本ファイルは PR-H2 では**どこからも import されない**。PR-H4a で各 sdk call site
 * が `import "@/lib/api/hey-api-interceptors"` を side-effect import で先頭に置いた
 * 時点で初めて module evaluation が走り interceptor が attach される。
 *
 * 設計判断:
 * - error interceptor は戻り値ではなく **`throw` で escape** させる。
 *   `client.gen.ts:189-208` の `throwOnError` 分岐を bypass し、呼び出し側が
 *   `throwOnError: true / false` のどちらでも一貫して ApiError を受け取れる
 * - publicClient 別 instance は作らず、auth interceptor 内で session null 時
 *   skip することで `typedPublic` 相当を実現する。logged-in user が anon endpoint
 *   を叩いたとき JWT が付くが backend は無視するので benign。
 *   ただし `"use cache"` 内で `getCurrentSession()` (cookies/headers 読取) が
 *   呼ばれる経路は PR-H4a で別途設計する (publicClient 別 instance か session
 *   lookup の swallow か)
 * - HMR 多重 attach 対策: `fns.length === 0` ガードで idempotent にする
 */

import "server-only";

import { ApiError, normalizeErrorDetail } from "@/lib/api/error";
import { buildInternalAuthHeaders } from "@/lib/api/internal-config";
import { getCurrentSession } from "@/lib/auth/guards";
import { client } from "@/types/client.gen";

if (client.interceptors.request.fns.length === 0) {
  client.interceptors.request.use(async (options) => {
    const session = await getCurrentSession();
    if (!session) return;
    const headers = await buildInternalAuthHeaders(session);
    for (const [k, v] of Object.entries(headers)) {
      options.headers.set(k, v);
    }
  });

  client.interceptors.error.use(async (error, response) => {
    const status = response?.status ?? 0;
    const detail =
      normalizeErrorDetail(error) || response?.statusText || `HTTP ${status}`;
    throw new ApiError(status, detail);
  });
}
