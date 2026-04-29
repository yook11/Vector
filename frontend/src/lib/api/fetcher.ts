import { ApiError, normalizeErrorDetail } from "@/lib/api/error";

const REQUEST_TIMEOUT_MS = 10_000;

export async function requestJson<T>(
  url: string,
  options?: RequestInit,
): Promise<T> {
  // backend hang で UI が無限ロードに張り付くのを防ぐため、固定 10 秒で
  // abort する。呼び出し側が独自 signal を渡してきた場合は AbortSignal.any
  // で OR-merge し、どちらの abort も尊重する。
  const timeoutController = new AbortController();
  const timeoutId = setTimeout(
    () => timeoutController.abort(),
    REQUEST_TIMEOUT_MS,
  );
  const signal = options?.signal
    ? AbortSignal.any([timeoutController.signal, options.signal])
    : timeoutController.signal;

  try {
    const res = await fetch(url, {
      ...options,
      signal,
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
  } catch (err) {
    // タイムアウト由来の AbortError は ApiError(408) に正規化して上層に
    // 伝える。呼び出し側が渡した external signal による abort はそのまま
    // 透過 (caller が自分で投げた abort なので caller が解釈する)。
    if (
      err instanceof Error &&
      err.name === "AbortError" &&
      timeoutController.signal.aborted
    ) {
      throw new ApiError(408, `Request timeout after ${REQUEST_TIMEOUT_MS}ms`);
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
}
