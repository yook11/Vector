"use server";

import { updateTag } from "next/cache";
import { requireAdminForAction } from "@/lib/auth/guards";
import { cacheTags } from "@/lib/cache/tags";
import { PositiveIdSchema } from "@/lib/validation/id";
import { deactivateSource as deactivateSourceSdk } from "@/types/sdk.gen";
import type { NewsSourceDetail } from "@/types/types.gen";
import { deactivateSourceCore } from "./source-cores";

/** Deactivate a news source (admin-only Server Action). */
export async function deactivateSource(id: number): Promise<NewsSourceDetail> {
  await requireAdminForAction();
  const validId = PositiveIdSchema.parse(id);
  const updated = await deactivateSourceCore(validId, deactivateSourceSdk);
  updateTag(cacheTags.sources);
  return updated;
}
