import { getServerSession } from "next-auth";
import type {
  CategoryListResponse,
  KeywordCategoryListResponse,
  KeywordCreate,
  KeywordListResponse,
  KeywordResponse,
  KeywordUpdate,
  NewsFetchRequest,
  NewsFetchResponse,
  NewsQuery,
  NewsResponse,
  PaginatedNewsResponse,
  SubscriptionListResponse,
  SubscriptionResponse,
  WatchlistListResponse,
  WatchlistResponse,
} from "@/types";
import { authOptions } from "@/lib/auth";

function getBaseUrl(): string {
  if (typeof window === "undefined") {
    // Server-side: prefer internal Docker URL for SSR
    const internal = process.env.INTERNAL_API_URL;
    if (internal) return internal;
    const pub = process.env.NEXT_PUBLIC_API_URL;
    if (pub) return pub;
    throw new Error(
      "[api-client] NEXT_PUBLIC_API_URL or INTERNAL_API_URL must be set",
    );
  }
  // Client-side: use public URL
  const pub = process.env.NEXT_PUBLIC_API_URL;
  if (!pub) {
    throw new Error("[api-client] NEXT_PUBLIC_API_URL must be set");
  }
  return pub;
}

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
  if (typeof window !== "undefined") return {};
  // Server-side: get access token from NextAuth session
  try {
    const session = await getServerSession(authOptions);
    if (session?.accessToken) {
      return { Authorization: `Bearer ${session.accessToken}` };
    }
  } catch {
    // Session not available (e.g., during build)
  }
  return {};
}

async function fetchApi<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const url = `${getBaseUrl()}${path}`;
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
  return fetchApi<PaginatedNewsResponse>(
    `/news${qs ? `?${qs}` : ""}`,
    { cache: "no-store" },
  );
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

// --- Subscriptions ---

/** Fetch user's keyword subscriptions. */
export async function getSubscriptions(): Promise<SubscriptionListResponse> {
  return fetchApi<SubscriptionListResponse>("/me/subscriptions", {
    cache: "no-store",
  });
}

/** Subscribe to a keyword. */
export async function subscribe(
  keywordId: number,
): Promise<SubscriptionResponse> {
  return fetchApi<SubscriptionResponse>("/me/subscriptions", {
    method: "POST",
    body: JSON.stringify({ keywordId }),
  });
}

/** Unsubscribe from a keyword. */
export async function unsubscribe(keywordId: number): Promise<void> {
  return fetchApi<void>(`/me/subscriptions/${keywordId}`, {
    method: "DELETE",
  });
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

/** Fetch all investment categories. */
export async function getCategories(
  locale?: string,
): Promise<CategoryListResponse> {
  const qs = locale ? `?locale=${locale}` : "";
  return fetchApi<CategoryListResponse>(`/categories${qs}`, {
    cache: "no-store",
  });
}

/** Fetch all keyword categories. */
export async function getKeywordCategories(
  locale?: string,
): Promise<KeywordCategoryListResponse> {
  const qs = locale ? `?locale=${locale}` : "";
  return fetchApi<KeywordCategoryListResponse>(`/keyword-categories${qs}`, {
    cache: "no-store",
  });
}

export { ApiError };
