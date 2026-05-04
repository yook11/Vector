"use server";

import { updateTag } from "next/cache";
import { requireAdminForAction } from "@/lib/auth/guards";
import { cacheTags } from "@/lib/cache/tags";
import { PositiveIdSchema } from "@/lib/validation/id";
import { activateSource as activateSourceSdk } from "@/types/sdk.gen";
import type { NewsSourceDetail } from "@/types/types.gen";
import { activateSourceCore } from "./source-cores";

/** Activate a news source (admin-only Server Action). */
export async function activateSource(id: number): Promise<NewsSourceDetail> {
  await requireAdminForAction();
  const validId = PositiveIdSchema.parse(id);
  const updated = await activateSourceCore(validId, activateSourceSdk);
  updateTag(cacheTags.sources);
  return updated;
}
