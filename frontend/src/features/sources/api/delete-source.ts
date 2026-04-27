"use client";

import { clientFetch } from "@/lib/api/client-fetcher";

export async function deleteSource(id: number): Promise<void> {
  return clientFetch<void>(`/admin/sources/${id}`, { method: "DELETE" });
}
