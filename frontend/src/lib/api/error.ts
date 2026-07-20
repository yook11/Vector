/**
 * Shared error types and helpers for hey-api error interceptor (hey-api-interceptors.ts).
 */

import type { ValidationError } from "@/types/types.gen";

export type InternalFetchErrorKind = "timeout" | "network";

export interface ApiErrorMeta {
  kind?: InternalFetchErrorKind | "http_429" | "http_5xx" | undefined;
  method?: string | undefined;
  path?: string | undefined;
  status?: number | undefined;
}

export class ApiError extends Error {
  declare body: unknown;
  declare retryAfter: string | null;

  constructor(
    public status: number,
    public detail: string,
    public meta?: ApiErrorMeta,
    body: unknown = undefined,
    retryAfter: string | null = null,
  ) {
    super(detail);
    this.name = "ApiError";
    Object.defineProperties(this, {
      body: {
        configurable: true,
        enumerable: false,
        value: body,
        writable: true,
      },
      retryAfter: {
        configurable: true,
        enumerable: false,
        value: retryAfter,
        writable: true,
      },
    });
  }
}

export class InternalFetchError extends Error {
  constructor(
    public kind: InternalFetchErrorKind,
    message: string,
  ) {
    super(message);
    this.name = "InternalFetchError";
  }
}

// Pydantic ValidationError の runtime narrow。types.gen.ts の shape
// (loc: (string|number)[], msg: string, type: string) のうち、UI 表示に
// 必須となる loc / msg のみを runtime 検証する。`in` operator narrowing で
// `as { ... }` cast を避け、generated 型と SSoT 接続する。
function isValidationError(e: unknown): e is ValidationError {
  if (e === null || typeof e !== "object") return false;
  if (!("loc" in e) || !Array.isArray(e.loc)) return false;
  if (!("msg" in e) || typeof e.msg !== "string") return false;
  return true;
}

/**
 * Normalize the `detail` field of a FastAPI error response.
 *
 * FastAPI returns two distinct shapes:
 * - Plain string: `{ "detail": "Not found" }` (HTTPException)
 * - Validation array: `{ "detail": [{loc, msg, type, ...}] }` (Pydantic)
 *
 * Returns a human-readable single-line message, or "" if the body has no
 * recognizable detail (caller should fall back to status text).
 */
export function normalizeErrorDetail(body: unknown): string {
  if (!body || typeof body !== "object" || !("detail" in body)) return "";
  // TS 4.9+ の `in` narrow により body.detail は unknown としてアクセス可。
  const detail: unknown = body.detail;

  if (typeof detail === "string") return detail;

  if (Array.isArray(detail)) {
    return detail
      .filter(isValidationError)
      .map((e) => {
        // loc is typically ["query", "fieldName"] or ["body", "nested", ...]
        // Skip the source prefix (first element) to show only the field path.
        const field = e.loc
          .slice(1)
          .map((part) => String(part))
          .join(".");
        return field ? `${field}: ${e.msg}` : e.msg;
      })
      .filter(Boolean)
      .join("; ");
  }

  return "";
}
