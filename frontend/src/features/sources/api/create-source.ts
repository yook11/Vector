"use server";

import { updateTag } from "next/cache";
import { serverFetch } from "@/lib/api/server-fetcher";
import { requireAdminForAction } from "@/lib/auth/guards";
import { cacheTags } from "@/lib/cache/tags";
import type { NewsSourceCreate, NewsSourceDetail } from "@/types";
import { NewSourceSchema } from "../schemas/source";
import { createSourceCore } from "./source-cores";

/** Create a news source (admin-only Server Action). */
export async function createSource(
  body: NewsSourceCreate,
): Promise<NewsSourceDetail> {
  await requireAdminForAction();
  // defense-in-depth: Client UI を bypass する hostile call で型が崩れた
  // payload が来うるので、SourceFormDialog と同じ zod schema で再検証する。
  const validBody = NewSourceSchema.parse(body);
  const created = await createSourceCore(validBody, serverFetch);
  updateTag(cacheTags.sources);
  return created;
}
