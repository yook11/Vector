"use server";

import { revalidateTag } from "next/cache";
import { serverFetch } from "@/lib/api/server-fetcher";
import { requireAdminForAction } from "@/lib/auth/guards";

/** Delete a news source (admin-only Server Action). */
export async function deleteSource(id: number): Promise<void> {
  await requireAdminForAction();
  await serverFetch<void>(`/admin/sources/${id}`, { method: "DELETE" });
  revalidateTag("sources", "max");
}
