import "server-only";

export type ServerLogLevel = "warn" | "error";

export type ServerLogEvent =
  | "frontend_internal_api_failure"
  | "frontend_auth_session_slow"
  | "frontend_auth_session_error"
  | "frontend_rate_limit_missing_ip"
  | "frontend_rate_limit_unknown_write"
  | "frontend_rate_limit_redis_client_error"
  | "frontend_rate_limit_redis_fail_open";

export interface ServerLogFields {
  method?: string | undefined;
  path?: string | undefined;
  kind?: string | undefined;
  status?: number | undefined;
  detail?: string | undefined;
  elapsedMs?: number | undefined;
  hasSession?: boolean | undefined;
  requestClass?: "sse" | "read" | "mutation" | undefined;
  errorType?: "unconfigured" | "connect" | "eval" | undefined;
}

function sanitizeFields(fields: ServerLogFields): ServerLogFields {
  if (!fields.path?.includes("?")) {
    return fields;
  }
  return { ...fields, path: fields.path.split("?")[0] };
}

export function logServerEvent(
  level: ServerLogLevel,
  event: ServerLogEvent,
  fields: ServerLogFields = {},
): void {
  const log = level === "error" ? console.error : console.warn;
  log(JSON.stringify({ event, level, ...sanitizeFields(fields) }));
}
