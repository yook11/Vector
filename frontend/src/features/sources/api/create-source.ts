"use client";

import { clientFetch } from "@/lib/api/client-fetcher";
import type { NewsSourceCreate, NewsSourceDetail } from "@/types";

export async function createSource(
  body: NewsSourceCreate,
): Promise<NewsSourceDetail> {
  return clientFetch<NewsSourceDetail>("/admin/sources", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
