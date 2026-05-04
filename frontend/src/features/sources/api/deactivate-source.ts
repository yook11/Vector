"use server";

import { updateTag } from "next/cache";
import { typedServer } from "@/lib/api/typed-server-fetcher";
import { requireAdminForAction } from "@/lib/auth/guards";
import { cacheTags } from "@/lib/cache/tags";
import { PositiveIdSchema } from "@/lib/validation/id";
import type { NewsSourceDetail } from "@/types/types.gen";
import { deactivateSourceCore } from "./source-cores";

/** Deactivate a news source (admin-only Server Action). */
export async function deactivateSource(id: number): Promise<NewsSourceDetail> {
  await requireAdminForAction();
  const validId = PositiveIdSchema.parse(id);
  const updated = await deactivateSourceCore(validId, typedServer);
  updateTag(cacheTags.sources);
  return updated;
}
