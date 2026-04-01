"use client";

import type {
  KeywordCreate,
  KeywordResponse,
  KeywordUpdate,
  NewsFetchRequest,
  NewsFetchResponse,
  NewsSourceCreate,
  NewsSourceDetail,
  NewsSourceDetailList,
} from "@/types";

class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
    this.name = "ApiError";
  }
}

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
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    if (res.status === 401) {
      window.location.href = "/auth/login";
      return undefined as T;
    }
    throw new ApiError(res.status, body.detail ?? res.statusText);
  }

  if (res.status === 204) return undefined as T;

  return res.json() as Promise<T>;
}

export async function clientAddToWatchlist(newsId: number): Promise<void> {
  await clientFetch("/me/watchlist", {
    method: "POST",
    body: JSON.stringify({ newsId }),
  });
}

export async function clientRemoveFromWatchlist(newsId: number): Promise<void> {
  await clientFetch(`/me/watchlist/${newsId}`, {
    method: "DELETE",
  });
}

// --- Keywords ---

export async function clientCreateKeyword(
  body: KeywordCreate,
): Promise<KeywordResponse> {
  return clientFetch<KeywordResponse>("/keywords", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function clientUpdateKeyword(
  id: number,
  body: KeywordUpdate,
): Promise<KeywordResponse> {
  return clientFetch<KeywordResponse>(`/keywords/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function clientDeleteKeyword(id: number): Promise<void> {
  return clientFetch<void>(`/keywords/${id}`, { method: "DELETE" });
}

// --- News ---

export async function clientTriggerFetch(
  body?: NewsFetchRequest,
): Promise<NewsFetchResponse> {
  return clientFetch<NewsFetchResponse>("/news/fetch", {
    method: "POST",
    body: JSON.stringify(body ?? {}),
  });
}

// --- Sources ---

export async function clientListSources(): Promise<NewsSourceDetailList> {
  return clientFetch<NewsSourceDetailList>("/sources");
}

export async function clientCreateSource(
  body: NewsSourceCreate,
): Promise<NewsSourceDetail> {
  return clientFetch<NewsSourceDetail>("/sources", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function clientDeleteSource(id: number): Promise<void> {
  return clientFetch<void>(`/sources/${id}`, { method: "DELETE" });
}

export async function clientToggleSource(
  id: number,
): Promise<NewsSourceDetail> {
  return clientFetch<NewsSourceDetail>(`/sources/${id}/toggle`, {
    method: "PATCH",
  });
}

export { ApiError };
