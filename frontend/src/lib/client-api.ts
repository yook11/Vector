"use client";

import { ApiError, normalizeErrorDetail } from "@/lib/api-error";
import type {
  FetchRequest,
  FetchResponse,
  NewsSourceCreate,
  NewsSourceDetail,
  NewsSourceDetailList,
} from "@/types";

async function clientFetch<T>(path: string, options?: RequestInit): Promise<T> {
  // All requests go through BFF proxy — no direct FastAPI access
  const url = `/api/proxy${path}`;

  const res = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
  });

  if (!res.ok) {
    const body = await res.json().catch(() => null);
    if (res.status === 401) {
      window.location.href = "/auth/login";
      return undefined as T;
    }
    const detail = normalizeErrorDetail(body) || res.statusText;
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return undefined as T;

  return res.json() as Promise<T>;
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

export async function clientToggleSource(
  id: number,
): Promise<NewsSourceDetail> {
  return clientFetch<NewsSourceDetail>(`/admin/sources/${id}/toggle`, {
    method: "PATCH",
  });
}

export { ApiError };
