"use server";

import { updateTag } from "next/cache";
import { requireAdminForAction } from "@/lib/auth/guards";
import { cacheTags } from "@/lib/cache/tags";
import { PositiveIdSchema } from "@/lib/validation/id";
import { deleteNewsSource as deleteNewsSourceSdk } from "@/types/sdk.gen";
import { deleteSourceCore } from "./source-cores";

/** Delete a news source (admin-only Server Action). */
export async function deleteSource(id: number): Promise<void> {
  await requireAdminForAction();
  const validId = PositiveIdSchema.parse(id);
  await deleteSourceCore(validId, deleteNewsSourceSdk);
  updateTag(cacheTags.sources);
}
