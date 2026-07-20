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
 * - `publicClient` (本ファイルで生成): user session は読まないが BFF 経由証明
 *   (user-less JWT) を付ける。`getCurrentSession()` (cookies/headers 読取) を
 *   踏まないので `"use cache"` 内 user 非依存 endpoint で `{ client: publicClient }`
 *   を per-call 渡せる。backend の require_bff_request が検証する
 *
 * 設計判断:
 * - error interceptor は戻り値ではなく **`throw` で escape** させる。
 *   `client.gen.ts` の `throwOnError` 分岐を bypass し、呼び出し側が
 *   `throwOnError: true / false` のどちらでも一貫して ApiError を受け取れる
 * - HMR 多重 attach 対策: `fns.length === 0` ガードで idempotent にする
 */

import "server-only";

import {
  ApiError,
  InternalFetchError,
  normalizeErrorDetail,
} from "@/lib/api/error";
import { createClientConfig } from "@/lib/api/hey-api.config";
import {
  buildBffRequestHeaders,
  buildInternalAuthHeaders,
} from "@/lib/api/internal-config";
import { getCurrentSession } from "@/lib/auth/guards";
import { logServerEvent } from "@/lib/observability/server-log";
import { createClient, createConfig } from "@/types/client";
import type { ResolvedRequestOptions } from "@/types/client/types.gen";
import { client } from "@/types/client.gen";
import type { ClientOptions } from "@/types/types.gen";

type RequestOptionsWithMethod = ResolvedRequestOptions & { method?: string };

function requestMetadata(options: ResolvedRequestOptions | undefined): {
  method?: string | undefined;
  path?: string | undefined;
} {
  const method =
    options &&
    "method" in options &&
    typeof (options as RequestOptionsWithMethod).method === "string"
      ? (options as RequestOptionsWithMethod).method
      : undefined;
  return { method, path: options?.url };
}

const errorInterceptor = async (
  error: unknown,
  response: Response | undefined,
  options: ResolvedRequestOptions | undefined,
) => {
  const { method, path } = requestMetadata(options);
  if (error instanceof InternalFetchError) {
    logServerEvent("error", "frontend_internal_api_failure", {
      kind: error.kind,
      method,
      path,
      detail: error.message,
    });
    throw new ApiError(0, error.message, { kind: error.kind, method, path });
  }

  const status = response?.status ?? 0;
  const detail =
    normalizeErrorDetail(error) || response?.statusText || `HTTP ${status}`;
  const body = response === undefined ? undefined : error;
  const retryAfter = response?.headers.get("Retry-After") ?? null;
  if (status === 429 || status >= 500) {
    const kind = status === 429 ? "http_429" : "http_5xx";
    logServerEvent(
      status >= 500 ? "error" : "warn",
      "frontend_internal_api_failure",
      {
        kind,
        method,
        path,
        status,
        detail,
      },
    );
    throw new ApiError(
      status,
      detail,
      { kind, method, path, status },
      body,
      retryAfter,
    );
  }

  throw new ApiError(
    status,
    detail,
    { method, path, status },
    body,
    retryAfter,
  );
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
 * user 非依存 endpoint 用 client。session を読まず BFF 経由証明 (user-less JWT)
 * だけを付けるため `"use cache"` 内 (cookies/headers 読取禁止) でも安全に使える。
 * SDK 関数 call で `{ client: publicClient }` を per-call 渡す。
 *
 * `createClientConfig(createConfig<ClientOptions>())` で defaults + baseUrl +
 * customFetch を一括適用。singleton 側と同じ runtime config を持つ。
 */
export const publicClient = createClient(
  createClientConfig(createConfig<ClientOptions>()),
);
if (publicClient.interceptors.request.fns.length === 0) {
  publicClient.interceptors.request.use(async (options) => {
    const headers = await buildBffRequestHeaders();
    for (const [k, v] of Object.entries(headers)) {
      options.headers.set(k, v);
    }
  });

  publicClient.interceptors.error.use(errorInterceptor);
}
