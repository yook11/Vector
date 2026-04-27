/**
 * Client-side fetcher (ブラウザから BFF プロキシ /api/proxy 経由で backend にアクセス)。
 *
 * - `path` を `/api/proxy${path}` に書き換え
 * - 401 を受けたら自動で `/auth/login` にリダイレクト
 *
 * features 側はこの関数だけを利用する。直接 `/api/proxy` を組み立てない。
 */

"use client";

import { requestJson } from "@/lib/api/fetcher";

export async function clientFetch<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  return requestJson<T>(`/api/proxy${path}`, options, {
    onUnauthorized: () => {
      window.location.href = "/auth/login";
    },
  });
}
