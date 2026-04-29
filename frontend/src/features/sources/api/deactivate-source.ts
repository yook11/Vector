"use server";

import { revalidateTag } from "next/cache";
import { serverFetch } from "@/lib/api/server-fetcher";
import { requireAdminForAction } from "@/lib/auth/guards";
import type { NewsSourceDetail } from "@/types";
import { deactivateSourceCore } from "./source-cores";

/** Deactivate a news source (admin-only Server Action). */
export async function deactivateSource(id: number): Promise<NewsSourceDetail> {
  await requireAdminForAction();
  const updated = await deactivateSourceCore(id, serverFetch);
  revalidateTag("sources", "max");
  return updated;
}
