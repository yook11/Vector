import { ApiError, normalizeErrorDetail } from "@/lib/api/error";

export async function requestJson<T>(
  url: string,
  options?: RequestInit,
): Promise<T> {
  const res = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
  });

  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = normalizeErrorDetail(body) || res.statusText;
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return undefined as T;

  return res.json() as Promise<T>;
}
