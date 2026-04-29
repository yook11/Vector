"use server";

import { updateTag } from "next/cache";
import { serverFetch } from "@/lib/api/server-fetcher";
import { requireAdminForAction } from "@/lib/auth/guards";
import type { NewsSourceCreate, NewsSourceDetail } from "@/types";
import { createSourceCore } from "./source-cores";

/** Create a news source (admin-only Server Action). */
export async function createSource(
  body: NewsSourceCreate,
): Promise<NewsSourceDetail> {
  await requireAdminForAction();
  const created = await createSourceCore(body, serverFetch);
  updateTag("sources");
  return created;
}
