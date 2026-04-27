/**
 * Shared error types and helpers for both server-side (api-client.ts)
 * and client-side (client-api.ts) fetchers.
 */

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
    this.name = "ApiError";
  }
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
  const detail = (body as { detail: unknown }).detail;

  if (typeof detail === "string") return detail;

  if (Array.isArray(detail)) {
    return detail
      .map((e) => {
        if (!e || typeof e !== "object") return "";
        const { loc, msg } = e as { loc?: unknown[]; msg?: string };
        // loc is typically ["query", "fieldName"] or ["body", "nested", ...]
        // Skip the source prefix (first element) to show only the field path.
        const field = Array.isArray(loc)
          ? loc
              .slice(1)
              .map((part) => String(part))
              .join(".")
          : "";
        return field && msg ? `${field}: ${msg}` : (msg ?? "");
      })
      .filter(Boolean)
      .join("; ");
  }

  return "";
}
