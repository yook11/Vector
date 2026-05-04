/**
 * hey-api client への interceptor 登録 + publicClient 別 instance の供給。
 *
 * Vector が必要とする 2 つの cross-cutting concern を attach する:
 * 1. auth: Better Auth session があれば HS256 JWT を Authorization header に注入
 * 2. error 正規化: backend が返す 4xx/5xx の生 JSON / text を ApiError(status, detail)
 *    に整形して throw (`throwOnError` フラグの設定値に依存させない)
 *
 * 2 つの client を export する:
 * - `client` (default singleton, types/client.gen.ts 由来): auth + error interceptor
 *   付き。auth-required endpoint (sources / watchlist) で per-call client なしに使う
 * - `publicClient` (本ファイルで生成): error interceptor のみ。auth interceptor を
 *   持たないので `getCurrentSession()` (cookies/headers 読取) を踏まない →
 *   `"use cache"` 内 anon endpoint で `{ client: publicClient }` を per-call 渡す
 *
 * 設計判断:
 * - error interceptor は戻り値ではなく **`throw` で escape** させる。
 *   `client.gen.ts` の `throwOnError` 分岐を bypass し、呼び出し側が
 *   `throwOnError: true / false` のどちらでも一貫して ApiError を受け取れる
 * - HMR 多重 attach 対策: `fns.length === 0` ガードで idempotent にする
 */

import "server-only";

import { ApiError, normalizeErrorDetail } from "@/lib/api/error";
import { createClientConfig } from "@/lib/api/hey-api.config";
import { buildInternalAuthHeaders } from "@/lib/api/internal-config";
import { getCurrentSession } from "@/lib/auth/guards";
import { createClient, createConfig } from "@/types/client";
import { client } from "@/types/client.gen";
import type { ClientOptions } from "@/types/types.gen";

// 自動生成の `client.gen.ts` は `createClient(createConfig<...>())` だけを
// 出力し、`runtimeConfigPath` で指定した `hey-api.config.ts` の
// `createClientConfig` (baseUrl + customFetch を返す) を wrap してくれない。
// その結果 baseUrl 未設定で SDK が相対 URL fetch → server-side で
// `Failed to parse URL` → response undefined → ApiError "HTTP 0" となる。
// 暫定として明示的に `setConfig` で runtime config を後付け注入する
// (openapi-ts 側の wiring が直ったら本行は撤去可能)。
client.setConfig(createClientConfig());

const errorInterceptor = async (
  error: unknown,
  response: Response | undefined,
) => {
  const status = response?.status ?? 0;
  const detail =
    normalizeErrorDetail(error) || response?.statusText || `HTTP ${status}`;
  throw new ApiError(status, detail);
};

if (client.interceptors.request.fns.length === 0) {
  client.interceptors.request.use(async (options) => {
    const session = await getCurrentSession();
    if (!session) return;
    const headers = await buildInternalAuthHeaders(session);
    for (const [k, v] of Object.entries(headers)) {
      options.headers.set(k, v);
    }
  });

  client.interceptors.error.use(errorInterceptor);
}

/**
 * anon endpoint 専用 client。auth interceptor を持たないため `"use cache"` 内
 * (cookies/headers 読取禁止) でも安全に使える。SDK 関数 call で
 * `{ client: publicClient }` を per-call 渡す。
 *
 * `createClientConfig(createConfig<ClientOptions>())` で defaults + baseUrl +
 * customFetch を一括適用。singleton 側と同じ runtime config を持つ。
 */
export const publicClient = createClient(
  createClientConfig(createConfig<ClientOptions>()),
);
if (publicClient.interceptors.error.fns.length === 0) {
  publicClient.interceptors.error.use(errorInterceptor);
}
