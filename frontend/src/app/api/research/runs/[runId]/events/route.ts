import "server-only";

import { getSessionCookie } from "better-auth/cookies";
import {
  buildInternalAuthHeaders,
  INTERNAL_API_URL,
} from "@/lib/api/internal-config";
import { auth } from "@/lib/auth/auth";
import { checkRateLimit, recordRateLimitSignal } from "@/lib/auth/rate-limit";
import { buildSseRateLimitPlan } from "@/lib/proxy/rate-limit-plan";

const NO_STORE = "no-store, no-transform";
const UPSTREAM_TIMEOUT_MS = 50_000;
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const STREAM_ID_PATTERN = /^(0|[1-9][0-9]*)-(0|[1-9][0-9]*)$/;
const STREAM_ID_PART_MAX = 18_446_744_073_709_551_615n;

interface ResearchRunEventsRouteContext {
  params: Promise<{ runId: string }>;
}

export async function GET(
  request: Request,
  { params }: ResearchRunEventsRouteContext,
): Promise<Response> {
  const { runId } = await params;
  const cursor = request.headers.get("Last-Event-ID");
  if (!isValidRunId(runId) || !isValidStreamId(cursor)) {
    return emptyResponse(400);
  }
  const normalizedRunId = runId.toLowerCase();

  const plan = buildSseRateLimitPlan({
    sessionIdentity: getSessionCookie(request),
    runId: normalizedRunId,
    flyClientIp: request.headers.get("fly-client-ip"),
    forwardedFor: request.headers.get("x-forwarded-for"),
    realIp: request.headers.get("x-real-ip"),
    isProduction: process.env.NODE_ENV === "production",
  });
  if (plan.signal) {
    recordRateLimitSignal(plan.signal);
  }
  const decision = await checkRateLimit(plan, { requestClass: "sse" });
  if (!decision.allowed) {
    return new Response(null, {
      status: 429,
      headers: {
        "Cache-Control": "no-store",
        "Retry-After": String(decision.retryAfterSeconds),
      },
    });
  }

  const session = await auth.api.getSession({ headers: request.headers });
  if (!session) {
    return emptyResponse(401);
  }

  const internalHeaders = await buildInternalAuthHeaders(session);
  const headers = new Headers(internalHeaders);
  headers.set("Accept", "text/event-stream");
  if (cursor !== null) {
    headers.set("Last-Event-ID", cursor);
  }
  const timeoutSignal = AbortSignal.timeout(UPSTREAM_TIMEOUT_MS);
  let upstream: Response;
  try {
    upstream = await fetch(
      new URL(
        `/api/v1/research/runs/${normalizedRunId}/events`,
        INTERNAL_API_URL,
      ).toString(),
      {
        method: "GET",
        headers,
        cache: "no-store",
        signal: AbortSignal.any([request.signal, timeoutSignal]),
      },
    );
  } catch {
    if (request.signal.aborted) {
      return emptyResponse(204);
    }
    return retryableUnavailableResponse();
  }

  const responseHeaders = new Headers({
    "Cache-Control": NO_STORE,
    "X-Accel-Buffering": "no",
  });
  const retryAfter = upstream.headers.get("Retry-After");
  if (retryAfter !== null) {
    responseHeaders.set("Retry-After", retryAfter);
  }
  if (upstream.status === 200) {
    responseHeaders.set(
      "Content-Type",
      upstream.headers.get("Content-Type") ??
        "text/event-stream; charset=utf-8",
    );
  }
  if (upstream.status === 204) {
    return new Response(null, { status: 204, headers: responseHeaders });
  }
  return new Response(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

function emptyResponse(status: number): Response {
  return new Response(null, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}

function retryableUnavailableResponse(): Response {
  return new Response(null, {
    status: 503,
    headers: {
      "Cache-Control": "no-store",
      "Retry-After": "5",
    },
  });
}

function isValidRunId(value: string): boolean {
  return UUID_PATTERN.test(value);
}

function isValidStreamId(value: string | null): boolean {
  if (value === null) return true;
  if (value.length > 41 || !STREAM_ID_PATTERN.test(value)) return false;
  const [milliseconds, sequence] = value.split("-");
  if (milliseconds === undefined || sequence === undefined) return false;
  return (
    BigInt(milliseconds) <= STREAM_ID_PART_MAX &&
    BigInt(sequence) <= STREAM_ID_PART_MAX
  );
}
