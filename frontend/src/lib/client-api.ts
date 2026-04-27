"use client";

import { ApiError } from "@/lib/api/error";
import { requestJson } from "@/lib/api/fetcher";
import type {
  FetchRequest,
  FetchResponse,
  NewsSourceCreate,
  NewsSourceDetail,
  NewsSourceDetailList,
} from "@/types";

async function clientFetch<T>(path: string, options?: RequestInit): Promise<T> {
  // All requests go through BFF proxy — no direct FastAPI access
  return requestJson<T>(`/api/proxy${path}`, options, {
    onUnauthorized: () => {
      window.location.href = "/auth/login";
    },
  });
}

export async function clientAddToWatchlist(articleId: number): Promise<void> {
  await clientFetch("/me/watchlist", {
    method: "POST",
    body: JSON.stringify({ articleId }),
  });
}

export async function clientRemoveFromWatchlist(
  articleId: number,
): Promise<void> {
  await clientFetch(`/me/watchlist/${articleId}`, {
    method: "DELETE",
  });
}

// --- News ---

export async function clientTriggerFetch(
  body?: FetchRequest,
): Promise<FetchResponse> {
  return clientFetch<FetchResponse>("/admin/pipeline/fetch", {
    method: "POST",
    body: JSON.stringify(body ?? {}),
  });
}

// --- Sources ---

export async function clientListSources(): Promise<NewsSourceDetailList> {
  return clientFetch<NewsSourceDetailList>("/admin/sources");
}

export async function clientCreateSource(
  body: NewsSourceCreate,
): Promise<NewsSourceDetail> {
  return clientFetch<NewsSourceDetail>("/admin/sources", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function clientDeleteSource(id: number): Promise<void> {
  return clientFetch<void>(`/admin/sources/${id}`, { method: "DELETE" });
}

export async function clientActivateSource(
  id: number,
): Promise<NewsSourceDetail> {
  return clientFetch<NewsSourceDetail>(`/admin/sources/${id}/activate`, {
    method: "PATCH",
  });
}

export async function clientDeactivateSource(
  id: number,
): Promise<NewsSourceDetail> {
  return clientFetch<NewsSourceDetail>(`/admin/sources/${id}/deactivate`, {
    method: "PATCH",
  });
}

export { ApiError };
