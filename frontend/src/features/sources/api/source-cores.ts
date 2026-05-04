/**
 * Server Action 内部の HTTP 構築ロジック (pure 関数群)。
 *
 * 副作用 (guard / updateTag) は wrapper 側の Server Action に残し、
 * ここでは fetcher を引数で受けて path / RequestInit を組み立てるだけにする。
 * これにより `vi.fn()` を fetcher として渡せばテスト可能 (Phase 1 の proxy 抽出
 * と同じ思想)。
 *
 * Strangler 移行で旧 `serverFetch` / `serverEmpty` から `typedServer`
 * (openapi-fetch ベース) に切り替え。path / method / body 型は generated.ts の
 * paths から自動導出される。watchlist-cores.ts が同じ pattern の exemplar。
 */

import {
  apiCall,
  apiVoid,
  type typedServer,
} from "@/lib/api/typed-server-fetcher";
import type { NewsSourceCreate, NewsSourceDetail } from "@/types";

export async function activateSourceCore(
  id: number,
  fetcher: typeof typedServer,
): Promise<NewsSourceDetail> {
  return apiCall(
    fetcher.PATCH("/api/v1/admin/sources/{source_id}/activate", {
      params: { path: { source_id: id } },
    }),
  );
}

export async function deactivateSourceCore(
  id: number,
  fetcher: typeof typedServer,
): Promise<NewsSourceDetail> {
  return apiCall(
    fetcher.PATCH("/api/v1/admin/sources/{source_id}/deactivate", {
      params: { path: { source_id: id } },
    }),
  );
}

export async function createSourceCore(
  body: NewsSourceCreate,
  fetcher: typeof typedServer,
): Promise<NewsSourceDetail> {
  return apiCall(
    fetcher.POST("/api/v1/admin/sources", {
      body,
    }),
  );
}

export async function deleteSourceCore(
  id: number,
  fetcher: typeof typedServer,
): Promise<void> {
  await apiVoid(
    fetcher.DELETE("/api/v1/admin/sources/{source_id}", {
      params: { path: { source_id: id } },
    }),
  );
}
