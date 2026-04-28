import { ApiError, normalizeErrorDetail } from "@/lib/api/error";

/**
 * 401 などの副作用フック (例: client-fetcher での `/auth/login` への redirect)。
 *
 * フックは「副作用を予約するだけ」で実行を止めない (`window.location.href` は
 * 同期的に navigate しない) ため、戻り値の型を曲げて `undefined` を流すと
 * フェッチ後に `.items` を触る利用側が runtime で死ぬ。401 は常に throw し、
 * 副作用だけフックで起こす契約に統一する。
 */
export type FetcherHooks = {
  onUnauthorized?: () => void;
};

export async function requestJson<T>(
  url: string,
  options?: RequestInit,
  hooks?: FetcherHooks,
): Promise<T> {
  const res = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
  });

  if (!res.ok) {
    const body = await res.json().catch(() => null);
    if (res.status === 401) {
      hooks?.onUnauthorized?.();
    }
    const detail = normalizeErrorDetail(body) || res.statusText;
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return undefined as T;

  return res.json() as Promise<T>;
}
