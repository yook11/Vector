"use server";

import { revalidateTag } from "next/cache";
import { serverFetch } from "@/lib/api/server-fetcher";
import { requireAdminForAction } from "@/lib/auth/guards";
import type { NewsSourceCreate, NewsSourceDetail } from "@/types";

/** Create a news source (admin-only Server Action). */
export async function createSource(
  body: NewsSourceCreate,
): Promise<NewsSourceDetail> {
  await requireAdminForAction();
  const created = await serverFetch<NewsSourceDetail>("/admin/sources", {
    method: "POST",
    body: JSON.stringify(body),
  });
  revalidateTag("sources", "max");
  return created;
}
