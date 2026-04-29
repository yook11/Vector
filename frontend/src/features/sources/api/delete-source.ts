"use server";

import { updateTag } from "next/cache";
import { serverEmpty } from "@/lib/api/server-fetcher";
import { requireAdminForAction } from "@/lib/auth/guards";
import { deleteSourceCore } from "./source-cores";

/** Delete a news source (admin-only Server Action). */
export async function deleteSource(id: number): Promise<void> {
  await requireAdminForAction();
  await deleteSourceCore(id, serverEmpty);
  updateTag("sources");
}
