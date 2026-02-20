"use client";

import { getSession } from "next-auth/react";

function getBaseUrl(): string {
  const pub = process.env.NEXT_PUBLIC_API_URL;
  return pub ?? "/api/mock";
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

export { ApiError };
