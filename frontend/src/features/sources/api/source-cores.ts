/**
 * Server Action 内部の HTTP 構築ロジック (pure 関数群)。
 *
 * 副作用 (guard / updateTag) は wrapper 側の Server Action に残し、
 * ここでは fetcher を引数で受けて path / RequestInit を組み立てるだけにする。
 * これにより `vi.fn()` を fetcher として渡せばテスト可能 (Phase 1 の proxy 抽出
 * と同じ思想)。
 */

import type { serverEmpty, serverFetch } from "@/lib/api/server-fetcher";
import type { NewsSourceCreate, NewsSourceDetail } from "@/types";

export async function activateSourceCore(
  id: number,
  fetcher: typeof serverFetch,
): Promise<NewsSourceDetail> {
  return fetcher<NewsSourceDetail>(`/admin/sources/${id}/activate`, {
    method: "PATCH",
  });
}

export async function deactivateSourceCore(
  id: number,
  fetcher: typeof serverFetch,
): Promise<NewsSourceDetail> {
  return fetcher<NewsSourceDetail>(`/admin/sources/${id}/deactivate`, {
    method: "PATCH",
  });
}

export async function createSourceCore(
  body: NewsSourceCreate,
  fetcher: typeof serverFetch,
): Promise<NewsSourceDetail> {
  return fetcher<NewsSourceDetail>("/admin/sources", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function deleteSourceCore(
  id: number,
  fetcher: typeof serverEmpty,
): Promise<void> {
  await fetcher(`/admin/sources/${id}`, { method: "DELETE" });
}
