/**
 * @hey-api/client-next の runtimeConfigPath が指す initial client config。
 *
 * `openapi-ts.config.ts` の `runtimeConfigPath` から相対参照される。client が
 * lazy 初期化されるとき `createClientConfig()` が呼ばれて baseUrl / customFetch
 * を注入する。auth header 注入と ApiError 正規化は PR-H2 で
 * `client.interceptors.{request,error}.use(...)` を別ファイルで attach する。
 *
 * baseUrl: openapi-fetch 経路と同じ origin のみ (`/api/v1` prefix は generated
 * の path key 側に含まれる)。
 *
 * customFetch: timeout 10s + AbortSignal merge。timeout は Error として throw
 * し、error interceptor (`hey-api-interceptors.ts`) で `ApiError(0, ...)` に
 * 包まれる。
 */

import "server-only";

import { INTERNAL_API_URL } from "@/lib/api/internal-config";
import type { CreateClientConfig } from "@/types/client.gen";

const REQUEST_TIMEOUT_MS = 10_000;

const customFetch: typeof fetch = async (input, init) => {
  const timeoutController = new AbortController();
  const timeoutId = setTimeout(
    () => timeoutController.abort(),
    REQUEST_TIMEOUT_MS,
  );
  const signal = init?.signal
    ? AbortSignal.any([init.signal, timeoutController.signal])
    : timeoutController.signal;

  try {
    return await fetch(input, { ...init, signal });
  } catch (err) {
    if (
      err instanceof Error &&
      err.name === "AbortError" &&
      timeoutController.signal.aborted
    ) {
      throw new Error(`Request timeout after ${REQUEST_TIMEOUT_MS}ms`);
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
};

export const createClientConfig: CreateClientConfig = (config) => ({
  ...config,
  baseUrl: new URL(INTERNAL_API_URL).origin,
  fetch: customFetch,
});
