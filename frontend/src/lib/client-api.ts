"use client";

import type {
  KeywordCreate,
  KeywordResponse,
  KeywordUpdate,
  NewsFetchRequest,
  NewsFetchResponse,
  NewsResponse,
  NewsSourceCreate,
  NewsSourceListResponse,
  NewsSourceResponse,
  NewsSourceUpdate,
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

export async function clientAddToWatchlist(
  newsArticleId: number,
): Promise<void> {
  await clientFetch("/me/watchlist", {
    method: "POST",
    body: JSON.stringify({ newsArticleId }),
  });
}

export async function clientRemoveFromWatchlist(
  newsArticleId: number,
): Promise<void> {
  await clientFetch(`/me/watchlist/${newsArticleId}`, {
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

/** Fetch all articles in a duplicate group. */
export async function clientGetGroupArticles(
  groupId: number,
): Promise<NewsResponse[]> {
  return clientFetch<NewsResponse[]>(`/news/groups/${groupId}`);
}

// --- Sources ---

export async function clientListSources(): Promise<NewsSourceListResponse> {
  return clientFetch<NewsSourceListResponse>("/sources");
}

export async function clientCreateSource(
  body: NewsSourceCreate,
): Promise<NewsSourceResponse> {
  return clientFetch<NewsSourceResponse>("/sources", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function clientUpdateSource(
  id: number,
  body: NewsSourceUpdate,
): Promise<NewsSourceResponse> {
  return clientFetch<NewsSourceResponse>(`/sources/${id}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export async function clientDeleteSource(id: number): Promise<void> {
  return clientFetch<void>(`/sources/${id}`, { method: "DELETE" });
}

export async function clientToggleSource(
  id: number,
): Promise<NewsSourceResponse> {
  return clientFetch<NewsSourceResponse>(`/sources/${id}/toggle`, {
    method: "PATCH",
  });
}

export { ApiError };
