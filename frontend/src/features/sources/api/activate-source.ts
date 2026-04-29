"use server";

import { revalidateTag } from "next/cache";
import { serverFetch } from "@/lib/api/server-fetcher";
import { requireAdminForAction } from "@/lib/auth/guards";
import type { NewsSourceDetail } from "@/types";
import { activateSourceCore } from "./source-cores";

/** Activate a news source (admin-only Server Action). */
export async function activateSource(id: number): Promise<NewsSourceDetail> {
  await requireAdminForAction();
  const updated = await activateSourceCore(id, serverFetch);
  revalidateTag("sources", "max");
  return updated;
}
