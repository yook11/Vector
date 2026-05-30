/**
 * Server Action 内部の HTTP 構築ロジック (pure 関数群)。
 *
 * 副作用 (guard / updateTag) は wrapper 側の Server Action に残し、
 * ここでは fetcher を引数で受けて path / body / RequestInit を組み立てるだけ
 * にし、`vi.fn()` fetcher でテスト可能にする。
 *
 * 各 fetcher は hey-api 生成の SDK 関数 (`activateSource` 等) と同じ signature
 * を持つ。`{ throwOnError: true }` を付けると戻り値の `data` フィールドが
 * `T | undefined` ではなく `T` に narrow される。auth header 注入は
 * side-effect import した `hey-api-interceptors` の singleton client 経由で実施される。
 */

import "@/lib/api/hey-api-interceptors";
import type {
  activateSource as activateSourceSdk,
  createNewsSource as createNewsSourceSdk,
  deactivateSource as deactivateSourceSdk,
  deleteNewsSource as deleteNewsSourceSdk,
} from "@/types/sdk.gen";
import type { NewsSourceCreate, NewsSourceDetail } from "@/types/types.gen";

export async function activateSourceCore(
  id: number,
  fetcher: typeof activateSourceSdk,
): Promise<NewsSourceDetail> {
  const { data } = await fetcher({
    throwOnError: true,
    path: { source_id: id },
  });
  return data;
}

export async function deactivateSourceCore(
  id: number,
  fetcher: typeof deactivateSourceSdk,
): Promise<NewsSourceDetail> {
  const { data } = await fetcher({
    throwOnError: true,
    path: { source_id: id },
  });
  return data;
}

export async function createSourceCore(
  body: NewsSourceCreate,
  fetcher: typeof createNewsSourceSdk,
): Promise<NewsSourceDetail> {
  const { data } = await fetcher({
    throwOnError: true,
    body,
  });
  return data;
}

export async function deleteSourceCore(
  id: number,
  fetcher: typeof deleteNewsSourceSdk,
): Promise<void> {
  await fetcher({
    throwOnError: true,
    path: { source_id: id },
  });
}
