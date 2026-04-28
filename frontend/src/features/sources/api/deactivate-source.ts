"use server";

import { revalidateTag } from "next/cache";
import { serverFetch } from "@/lib/api/server-fetcher";
import { requireAdminForAction } from "@/lib/auth/guards";
import type { NewsSourceDetail } from "@/types";

/** Deactivate a news source (admin-only Server Action). */
export async function deactivateSource(id: number): Promise<NewsSourceDetail> {
  await requireAdminForAction();
  const updated = await serverFetch<NewsSourceDetail>(
    `/admin/sources/${id}/deactivate`,
    { method: "PATCH" },
  );
  revalidateTag("sources", "max");
  return updated;
}
