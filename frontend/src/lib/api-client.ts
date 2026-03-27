import { headers } from "next/headers";
import { auth } from "@/lib/auth";
import type {
  CategoryDetailListResponse,
  KeywordCreate,
  KeywordListResponse,
  KeywordResponse,
  KeywordUpdate,
  NewsFetchRequest,
  NewsFetchResponse,
  NewsQuery,
  NewsResponse,
  NewsSourceListResponse,
  PaginatedNewsResponse,
  WatchlistListResponse,
  WatchlistResponse,
} from "@/types";

const INTERNAL_API_URL =
  process.env.INTERNAL_API_URL ?? "http://localhost:8000/api/v1";

const INTERNAL_SECRET =
  process.env.INTERNAL_API_SECRET ?? "change-me-in-production";

class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
    this.name = "ApiError";
  }
}

async function getAuthHeaders(): Promise<Record<string, string>> {
  try {
    const session = await auth.api.getSession({
      headers: await headers(),
    });
    if (session) {
      return {
        "X-User-ID": session.user.id,
        "X-User-Role":
          ((session.user as Record<string, unknown>).role as string) ?? "user",
        "X-Internal-Secret": INTERNAL_SECRET,
      };
    }
  } catch {
    // Session not available (e.g., during build)
  }
  return {};
}

async function fetchApi<T>(path: string, options?: RequestInit): Promise<T> {
  const url = `${INTERNAL_API_URL}${path}`;
  const authHeaders = await getAuthHeaders();
  const res = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders,
      ...options?.headers,
    },
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, body.detail ?? res.statusText);
  }

  // 204 No Content
  if (res.status === 204) return undefined as T;

  return res.json() as Promise<T>;
}

/** Fetch paginated news list with optional filters. */
export async function getNews(
  query?: NewsQuery,
): Promise<PaginatedNewsResponse> {
  const params = new URLSearchParams();
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) params.set(key, String(value));
    }
  }
  const qs = params.toString();
  return fetchApi<PaginatedNewsResponse>(`/news${qs ? `?${qs}` : ""}`, {
    cache: "no-store",
  });
}

/** Fetch a single news article by ID. */
export async function getNewsById(id: number): Promise<NewsResponse> {
  return fetchApi<NewsResponse>(`/news/${id}`, { cache: "no-store" });
}

/** Trigger a manual news fetch. */
export async function triggerFetch(
  body?: NewsFetchRequest,
): Promise<NewsFetchResponse> {
  return fetchApi<NewsFetchResponse>("/news/fetch", {
    method: "POST",
    body: JSON.stringify(body ?? {}),
  });
}

/** Fetch all keywords. */
export async function getKeywords(): Promise<KeywordListResponse> {
  return fetchApi<KeywordListResponse>("/keywords", { cache: "no-store" });
}

/** Create a new keyword. */
export async function createKeyword(
  body: KeywordCreate,
): Promise<KeywordResponse> {
  return fetchApi<KeywordResponse>("/keywords", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Update a keyword. */
export async function updateKeyword(
  id: number,
  body: KeywordUpdate,
): Promise<KeywordResponse> {
  return fetchApi<KeywordResponse>(`/keywords/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

/** Delete a keyword. */
export async function deleteKeyword(id: number): Promise<void> {
  return fetchApi<void>(`/keywords/${id}`, { method: "DELETE" });
}

// --- Watchlist ---

/** Fetch user's watchlist. */
export async function getWatchlist(
  page = 1,
  perPage = 20,
): Promise<WatchlistListResponse> {
  return fetchApi<WatchlistListResponse>(
    `/me/watchlist?page=${page}&perPage=${perPage}`,
    { cache: "no-store" },
  );
}

/** Add an article to the watchlist. */
export async function addToWatchlist(
  newsArticleId: number,
): Promise<WatchlistResponse> {
  return fetchApi<WatchlistResponse>("/me/watchlist", {
    method: "POST",
    body: JSON.stringify({ newsArticleId }),
  });
}

/** Remove an article from the watchlist. */
export async function removeFromWatchlist(
  newsArticleId: number,
): Promise<void> {
  return fetchApi<void>(`/me/watchlist/${newsArticleId}`, {
    method: "DELETE",
  });
}

/** Fetch articles semantically similar to the given article. */
export async function getSimilarNews(
  id: number,
  limit = 5,
): Promise<NewsResponse[]> {
  return fetchApi<NewsResponse[]>(`/news/${id}/similar?limit=${limit}`, {
    cache: "no-store",
  });
}

// --- Categories ---

/** Fetch all categories (unified — keywords + article counts). */
export async function getCategories(): Promise<CategoryDetailListResponse> {
  return fetchApi<CategoryDetailListResponse>("/categories", {
    cache: "no-store",
  });
}

// --- News Sources ---

/** Fetch all news sources (SSR-compatible). */
export async function getSources(): Promise<NewsSourceListResponse> {
  return fetchApi<NewsSourceListResponse>("/sources", { cache: "no-store" });
}

export { ApiError };
