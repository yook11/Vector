"use client";

import { clientFetch } from "@/lib/api/client-fetcher";
import type { NewsSourceDetail } from "@/types";

export async function deactivateSource(id: number): Promise<NewsSourceDetail> {
  return clientFetch<NewsSourceDetail>(`/admin/sources/${id}/deactivate`, {
    method: "PATCH",
  });
}
