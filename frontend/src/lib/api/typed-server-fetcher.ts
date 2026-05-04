/**
 * Type-safe server fetcher (Vector 唯一の backend 経路)。
 *
 * openapi-fetch を介して `src/types/generated.ts` の `paths` から response
 * 型を自動導出することで、path と response 型を構造的に連結する (旧
 * `serverFetch<T>(path)` のような手書き T assert を排除)。
 *
 * Vector 固有の以下を middleware と custom fetch で保持する:
 * - per-request HS256 JWT 認証 (`buildInternalAuthHeaders` を再利用、
 *   `typedServer` のみ。`typedPublic` は header を一切付与しない)
 * - 4xx/5xx を `ApiError` に正規化して throw (FastAPI HTTPException + Pydantic
 *   ValidationError 両 shape を `normalizeErrorDetail` で吸収)
 * - 10s timeout + AbortSignal merge → `ApiError(408)`
 */

import "server-only";

import createClient, { type Middleware } from "openapi-fetch";
import { ApiError, normalizeErrorDetail } from "@/lib/api/error";
import {
  buildInternalAuthHeaders,
  INTERNAL_API_URL,
} from "@/lib/api/internal-config";
import { getCurrentSession } from "@/lib/auth/guards";
import type { paths } from "@/types/generated";

const REQUEST_TIMEOUT_MS = 10_000;

// openapi-fetch の baseUrl は origin のみ。`INTERNAL_API_URL` に含まれる
// `/api/v1` は generated.ts の paths キー側 (`/api/v1/...`) に既に含まれている
// ため、ここで重ねて付けると二重 prefix で 404 になる。
const OPENAPI_BASE_URL = new URL(INTERNAL_API_URL).origin;

// openapi-fetch の `fetch` slot で timeout + AbortSignal merge を担う。
// `fetcher.ts` の `executeRequest` と等価の logic。
const customFetch = async (input: Request): Promise<Response> => {
  const timeoutController = new AbortController();
  const timeoutId = setTimeout(
    () => timeoutController.abort(),
    REQUEST_TIMEOUT_MS,
  );
  const signal = input.signal
    ? AbortSignal.any([timeoutController.signal, input.signal])
    : timeoutController.signal;

  try {
    return await fetch(input, { signal });
  } catch (err) {
    if (
      err instanceof Error &&
      err.name === "AbortError" &&
      timeoutController.signal.aborted
    ) {
      throw new ApiError(408, `Request timeout after ${REQUEST_TIMEOUT_MS}ms`);
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
};

// Better Auth session から HS256 JWT を取得し Authorization に注入する。
// session が無いときは何も付与しない (= public client と同じ振る舞い)。
const authMiddleware: Middleware = {
  async onRequest({ request }) {
    const session = await getCurrentSession();
    if (!session) return;
    const headers = await buildInternalAuthHeaders(session);
    for (const [k, v] of Object.entries(headers)) {
      request.headers.set(k, v);
    }
    return request;
  },
};

// non-2xx を ApiError に正規化して throw する middleware。successful path で
// `result.data` が必ず存在することを保証するための要 (apiCall / apiVoid 側で
// `result.error !== undefined` の defensive check はあるが、normal flow は
// この middleware で落とす)。
const errorMiddleware: Middleware = {
  async onResponse({ response }) {
    if (response.ok) return;
    const body = await response
      .clone()
      .json()
      .catch(() => null);
    const detail = normalizeErrorDetail(body) || response.statusText;
    throw new ApiError(response.status, detail);
  },
};

const authedClient = createClient<paths>({
  baseUrl: OPENAPI_BASE_URL,
  fetch: customFetch,
});
authedClient.use(authMiddleware, errorMiddleware);

const publicClient = createClient<paths>({
  baseUrl: OPENAPI_BASE_URL,
  fetch: customFetch,
});
publicClient.use(errorMiddleware);

/**
 * Type-safe Server fetcher (auth 付き)。openapi-fetch の client をそのまま
 * export する。callers は `apiCall` / `apiVoid` で `{data, error}` shape を
 * unwrap する。
 */
export { authedClient as typedServer, publicClient as typedPublic };

/**
 * `typedServer.GET(...)` 等の戻り値 (`{data, error, response}`) を unwrap して
 * `Promise<T>` に揃える helper。errorMiddleware が non-ok を throw するので、
 * ここに来た時点で `data` は必ず存在する (型上は `T | undefined` だが実 runtime
 * は非 undefined)。
 *
 * @example
 * ```ts
 * const data = await apiCall(typedServer.GET("/api/v1/me/watchlist/ids", {
 *   next: { tags: ["watchlist:me"] },
 * }));
 * return new Set(data.ids);
 * ```
 */
export async function apiCall<
  R extends { data?: unknown; error?: unknown; response: Response },
>(promise: Promise<R>): Promise<NonNullable<R["data"]>> {
  const result = await promise;
  if (result.error !== undefined) {
    // errorMiddleware を通って throw されているはずで、ここに来るのは defensive。
    const detail =
      normalizeErrorDetail(result.error) || result.response.statusText;
    throw new ApiError(result.response.status, detail);
  }
  return result.data as NonNullable<R["data"]>;
}

/**
 * 204 No Content endpoint (POST/DELETE 系で response body なし) 用。
 * `apiCall` と異なり戻り値を返さない。
 */
export async function apiVoid<
  R extends { error?: unknown; response: Response },
>(promise: Promise<R>): Promise<void> {
  const result = await promise;
  if (result.error !== undefined) {
    const detail =
      normalizeErrorDetail(result.error) || result.response.statusText;
    throw new ApiError(result.response.status, detail);
  }
}
