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
 * customFetch: timeout 15s + AbortSignal merge。response 不在の失敗は
 * `InternalFetchError` に分類し、error interceptor 側で診断ログに載せる。
 */

import "server-only";

import { InternalFetchError } from "@/lib/api/error";
import { INTERNAL_API_URL } from "@/lib/api/internal-config";
import type { CreateClientConfig } from "@/types/client.gen";

// cold start 吸収の暫定値。vector-core API process の autostop による cold start
// (実測 ~11-12s) が frontend の fetch timeout と衝突して frontend_internal_api_failure
// (kind:timeout) を出していたため延長した対症対応。根治は backend を warm 維持する
// こと (min_machines_running=1 等) で、本値はそれまでの暫定。
const REQUEST_TIMEOUT_MS = 15_000;

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
      throw new InternalFetchError(
        "timeout",
        `Request timeout after ${REQUEST_TIMEOUT_MS}ms`,
      );
    }
    if (err instanceof Error) {
      throw new InternalFetchError("network", err.message);
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
