"use server";

import { updateTag } from "next/cache";
import { serverFetch } from "@/lib/api/server-fetcher";
import { requireAdminForAction } from "@/lib/auth/guards";
import { PositiveIdSchema } from "@/lib/validation/id";
import type { NewsSourceDetail } from "@/types";
import { deactivateSourceCore } from "./source-cores";

/** Deactivate a news source (admin-only Server Action). */
export async function deactivateSource(id: number): Promise<NewsSourceDetail> {
  await requireAdminForAction();
  const validId = PositiveIdSchema.parse(id);
  const updated = await deactivateSourceCore(validId, serverFetch);
  updateTag("sources");
  return updated;
}
