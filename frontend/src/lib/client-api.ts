"use client";

import { getSession, signOut } from "next-auth/react";
import type {
  KeywordCreate,
  KeywordResponse,
  KeywordUpdate,
  NewsFetchRequest,
  NewsFetchResponse,
} from "@/types";

function getBaseUrl(): string {
  const pub = process.env.NEXT_PUBLIC_API_URL;
  if (!pub) {
    throw new Error("[client-api] NEXT_PUBLIC_API_URL must be set");
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

async function clientFetch<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const url = `${getBaseUrl()}${path}`;

  const session = await getSession();
  const authHeaders: Record<string, string> = {};
  if (session?.accessToken) {
    authHeaders.Authorization = `Bearer ${session.accessToken}`;
  }

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
    if (res.status === 401) {
      await signOut({ callbackUrl: "/auth/login" });
      return undefined as T;
    }
    throw new ApiError(res.status, body.detail ?? res.statusText);
  }

  if (res.status === 204) return undefined as T;

  return res.json() as Promise<T>;
}

export async function clientSubscribe(keywordId: number): Promise<void> {
  await clientFetch("/me/subscriptions", {
    method: "POST",
    body: JSON.stringify({ keywordId }),
  });
}

export async function clientUnsubscribe(keywordId: number): Promise<void> {
  await clientFetch(`/me/subscriptions/${keywordId}`, {
    method: "DELETE",
  });
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

export { ApiError };
