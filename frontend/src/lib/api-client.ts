import { headers } from "next/headers";
import { ApiError, normalizeErrorDetail } from "@/lib/api-error";
import { auth } from "@/lib/auth";
import type {
  ArticleBrief,
  ArticleDetail,
  ArticleQuery,
  CategoryDetailListResponse,
  FetchRequest,
  FetchResponse,
  NewsSourceDetailList,
  PaginatedArticleResponse,
  SemanticSearchQuery,
} from "@/types";

const INTERNAL_API_URL =
  process.env.INTERNAL_API_URL ?? "http://localhost:8000/api/v1";

const INTERNAL_SECRET =
  process.env.INTERNAL_API_SECRET ?? "change-me-in-production";

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
    const body = await res.json().catch(() => null);
    const detail = normalizeErrorDetail(body) || res.statusText;
    throw new ApiError(res.status, detail);
  }

  // 204 No Content
  if (res.status === 204) return undefined as T;

  return res.json() as Promise<T>;
}

/** Fetch paginated article list with optional filters. */
export async function getArticles(
  query?: ArticleQuery,
): Promise<PaginatedArticleResponse> {
  const params = new URLSearchParams();
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) params.set(key, String(value));
    }
  }
  const qs = params.toString();
  return fetchApi<PaginatedArticleResponse>(`/articles${qs ? `?${qs}` : ""}`, {
    cache: "no-store",
  });
}

/** Search articles by semantic similarity. */
export async function searchArticles(
  query: SemanticSearchQuery,
): Promise<PaginatedArticleResponse> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) params.set(key, String(value));
  }
  return fetchApi<PaginatedArticleResponse>(
    `/articles/search?${params.toString()}`,
    { cache: "no-store" },
  );
}

/** Fetch a single article by ID. */
export async function getArticleById(id: number): Promise<ArticleDetail> {
  return fetchApi<ArticleDetail>(`/articles/${id}`, { cache: "no-store" });
}

/** Trigger a manual news fetch. */
export async function triggerFetch(
  body?: FetchRequest,
): Promise<FetchResponse> {
  return fetchApi<FetchResponse>("/admin/pipeline/fetch", {
    method: "POST",
    body: JSON.stringify(body ?? {}),
  });
}

// --- Watchlist ---

/** Fetch user's watchlist. */
export async function getWatchlist(
  page = 1,
  perPage = 20,
): Promise<PaginatedArticleResponse> {
  return fetchApi<PaginatedArticleResponse>(
    `/me/watchlist?page=${page}&perPage=${perPage}`,
    { cache: "no-store" },
  );
}

/** Add an article to the watchlist. */
export async function addToWatchlist(articleId: number): Promise<void> {
  return fetchApi<void>("/me/watchlist", {
    method: "POST",
    body: JSON.stringify({ articleId }),
  });
}

/** Remove an article from the watchlist. */
export async function removeFromWatchlist(articleId: number): Promise<void> {
  return fetchApi<void>(`/me/watchlist/${articleId}`, {
    method: "DELETE",
  });
}

/** Fetch articles semantically similar to the given article. */
export async function getSimilarArticles(
  id: number,
  limit = 5,
): Promise<ArticleBrief[]> {
  return fetchApi<ArticleBrief[]>(`/articles/${id}/similar?limit=${limit}`, {
    cache: "no-store",
  });
}

// --- Categories ---

/** Fetch all categories (unified — topics + article counts). */
export async function getCategories(): Promise<CategoryDetailListResponse> {
  return fetchApi<CategoryDetailListResponse>("/categories", {
    cache: "no-store",
  });
}

// --- News Sources ---

/** Fetch all news sources (SSR-compatible). */
export async function getSources(): Promise<NewsSourceDetailList> {
  return fetchApi<NewsSourceDetailList>("/admin/sources", {
    cache: "no-store",
  });
}

export { ApiError };
