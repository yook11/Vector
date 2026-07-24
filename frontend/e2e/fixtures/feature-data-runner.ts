import { type ChildProcess, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { once } from "node:events";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import http, { type IncomingMessage, type ServerResponse } from "node:http";
import https from "node:https";
import net from "node:net";
import path from "node:path";

const FRONTEND_DIRECTORY = path.resolve(__dirname, "../..");
const RUNNER_DIRECTORY = path.join(FRONTEND_DIRECTORY, ".e2e-next");
const GATE_TIMEOUT_MS = 30_000;
const ROUTE_READY_TIMEOUT_MS = 90_000;
const MAX_CHILD_OUTPUT_LENGTH = 512_000;
const ANSI_ESCAPE_SEQUENCE = new RegExp(
  `${String.fromCharCode(27)}\\[[0-9;]*m`,
  "g",
);

type BackendGate = {
  waitForHit: () => Promise<void>;
  release: () => void;
  hitCount: () => number;
};

type RunningProxy = {
  url: string;
  gate: BackendGate;
  armGate: () => void;
  close: () => Promise<void>;
};

type RunningFrontend = {
  child: ChildProcess;
  diagnostics: () => string;
};

export type FeatureDataRunner = {
  baseURL: string;
  gate: BackendGate;
  diagnostics: () => string;
  dispose: () => Promise<void>;
};

export type FeatureDataRunnerOptions = {
  scenario: string;
  /** fresh childがcompileとHTTP responseを完了すべきfrontend route。 */
  readyPathname: string;
  /** route-ready probeへ付与するPlaywright storage state。frontend相対path。 */
  storageStatePath: string;
  /** backend で一度だけ止める GET endpoint の pathname。 */
  heldPathname: string;
  /** shell / fallback を観測できる最小保持時間。 */
  holdMs?: number;
  /** release後にupstreamへ送らず返す決定的なbackend outcome。 */
  response?: {
    status: number;
    body: string;
    contentType?: string;
  };
};

function isWithinDirectory(directory: string, candidate: string): boolean {
  const relative = path.relative(directory, candidate);
  return (
    relative.length > 0 &&
    !relative.startsWith(`..${path.sep}`) &&
    relative !== ".."
  );
}

function scenarioDirectory(scenario: string): string {
  if (!/^[a-z0-9-]+$/.test(scenario)) {
    throw new Error(`Invalid feature-data scenario name: ${scenario}`);
  }
  const directory = path.resolve(
    RUNNER_DIRECTORY,
    `${scenario}-${process.pid}-${randomUUID()}`,
  );
  if (!isWithinDirectory(RUNNER_DIRECTORY, directory)) {
    throw new Error(
      "Refusing to create feature-data runner outside its directory",
    );
  }
  return directory;
}

async function reservePort(): Promise<number> {
  const server = net.createServer();
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve());
  });
  const address = server.address();
  await new Promise<void>((resolve, reject) => {
    server.close((error) => (error === undefined ? resolve() : reject(error)));
  });
  if (address === null || typeof address === "string") {
    throw new Error("Failed to reserve a TCP port for feature-data runner");
  }
  return address.port;
}

function forwardToBackend(
  request: IncomingMessage,
  response: ServerResponse,
  upstreamOrigin: URL,
): void {
  const target = new URL(request.url ?? "/", upstreamOrigin);
  const requester = target.protocol === "https:" ? https.request : http.request;
  const upstreamRequest = requester(
    target,
    {
      headers: { ...request.headers, host: target.host },
      method: request.method,
    },
    (upstreamResponse) => {
      response.writeHead(
        upstreamResponse.statusCode ?? 502,
        upstreamResponse.statusMessage,
        upstreamResponse.headers,
      );
      upstreamResponse.pipe(response);
    },
  );
  upstreamRequest.once("error", (error) => {
    if (!response.headersSent) {
      response.writeHead(502, { "content-type": "text/plain; charset=utf-8" });
    }
    response.end(`Feature-data proxy request failed: ${error.message}`);
  });
  request.pipe(upstreamRequest);
}

function withTimeout(
  promise: Promise<void>,
  label: string | (() => string),
): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(
      () => reject(new Error(typeof label === "function" ? label() : label)),
      GATE_TIMEOUT_MS,
    );
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
  });
}

async function startProxy(
  heldPathname: string,
  holdMs: number,
  fixedResponse?: FeatureDataRunnerOptions["response"],
): Promise<RunningProxy> {
  const rawUpstream = process.env.INTERNAL_API_URL;
  if (rawUpstream === undefined || rawUpstream.length === 0) {
    throw new Error(
      "Feature-data runner requires INTERNAL_API_URL in its process environment",
    );
  }
  const upstreamOrigin = new URL(rawUpstream);
  if (
    upstreamOrigin.protocol !== "http:" &&
    upstreamOrigin.protocol !== "https:"
  ) {
    throw new Error(
      "Feature-data runner only supports http(s) INTERNAL_API_URL",
    );
  }

  let releaseGate: (() => void) | undefined;
  const release = new Promise<void>((resolve) => {
    releaseGate = resolve;
  });
  let registerHit: (() => void) | undefined;
  const hit = new Promise<void>((resolve) => {
    registerHit = resolve;
  });
  let hitCount = 0;
  let held = false;
  let holdElapsed: Promise<void> | undefined;
  let gateArmed = false;
  const observedPathnames: string[] = [];

  const server = http.createServer(async (request, response) => {
    const pathname = new URL(request.url ?? "/", "http://feature-data.local")
      .pathname;
    observedPathnames.push(pathname);
    if (observedPathnames.length > 20) observedPathnames.shift();
    if (gateArmed && request.method === "GET" && pathname === heldPathname) {
      if (!held) {
        held = true;
        hitCount += 1;
        holdElapsed = new Promise<void>((resolve) =>
          setTimeout(resolve, holdMs),
        );
        registerHit?.();
      }
      await Promise.all([release, holdElapsed]);
      if (fixedResponse !== undefined) {
        response.writeHead(fixedResponse.status, {
          "cache-control": "no-store",
          "content-type":
            fixedResponse.contentType ?? "application/json; charset=utf-8",
        });
        response.end(fixedResponse.body);
        return;
      }
    }
    forwardToBackend(request, response, upstreamOrigin);
  });

  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve());
  });
  const address = server.address();
  if (address === null || typeof address === "string") {
    await new Promise<void>((resolve, reject) => {
      server.close((error) =>
        error === undefined ? resolve() : reject(error),
      );
    });
    throw new Error("Feature-data proxy did not expose a TCP port");
  }

  return {
    url: `http://127.0.0.1:${address.port}`,
    armGate: () => {
      gateArmed = true;
    },
    gate: {
      waitForHit: () =>
        withTimeout(hit, () =>
          [
            `Expected backend request for ${heldPathname} did not reach the feature-data gate`,
            `Observed backend requests: ${observedPathnames.join(", ") || "(none)"}`,
          ].join("\n"),
        ),
      release: () => releaseGate?.(),
      hitCount: () => hitCount,
    },
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.close((error) =>
          error === undefined ? resolve() : reject(error),
        );
      }),
  };
}

function startFrontend(
  distDirectory: string,
  fontResponsesPath: string,
  port: number,
  proxyUrl: string,
): RunningFrontend {
  const configuredDistDirectory = path.relative(
    FRONTEND_DIRECTORY,
    distDirectory,
  );
  if (
    configuredDistDirectory.length === 0 ||
    configuredDistDirectory === ".." ||
    configuredDistDirectory.startsWith(`..${path.sep}`)
  ) {
    throw new Error(
      "Feature-data distDir must stay inside the frontend project",
    );
  }
  const child = spawn(
    process.execPath,
    [
      "./node_modules/next/dist/bin/next",
      "dev",
      "--webpack",
      "-p",
      String(port),
    ],
    {
      cwd: FRONTEND_DIRECTORY,
      env: {
        ...process.env,
        INTERNAL_API_URL: proxyUrl,
        E2E_NEXT_DIST_DIR: configuredDistDirectory,
        NEXT_FONT_GOOGLE_MOCKED_RESPONSES: fontResponsesPath,
        NEXT_TELEMETRY_DISABLED: "1",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let output = "";
  const collect = (source: "stderr" | "stdout", chunk: Buffer | string) => {
    if (output.length >= MAX_CHILD_OUTPUT_LENGTH) return;
    const remaining = MAX_CHILD_OUTPUT_LENGTH - output.length;
    output += `[${source}] ${String(chunk).slice(0, remaining)}`;
  };
  child.stdout?.on("data", (chunk: Buffer | string) =>
    collect("stdout", chunk),
  );
  child.stderr?.on("data", (chunk: Buffer | string) =>
    collect("stderr", chunk),
  );

  return {
    child,
    diagnostics: () => {
      const normalized = output
        .replaceAll(ANSI_ESCAPE_SEQUENCE, "")
        .split(/\r?\n/);
      const actionable = normalized.flatMap((line, index) =>
        !line.includes(
          "Failed to find font override values for font `Big Shoulders`",
        ) &&
        /\b(?:error|failed|failure|unhandled|cannot|eaddrinuse)\b/i.test(line)
          ? [index]
          : [],
      );
      if (actionable.length === 0)
        return normalized.slice(-80).join("\n").trim();
      const first = actionable[0];
      const last = actionable.at(-1);
      if (first === undefined || last === undefined) return "";
      const firstBlock = normalized.slice(first, first + 40);
      const lastBlock =
        last === first
          ? []
          : [
              "[last actionable child output]",
              ...normalized.slice(last, last + 40),
            ];
      return [...firstBlock, ...lastBlock].join("\n").trim();
    },
  };
}

type StorageState = {
  cookies?: Array<{ name?: unknown; value?: unknown; domain?: unknown }>;
};

async function storageStateCookieHeader(storageStatePath: string) {
  const storageStateFile = path.resolve(FRONTEND_DIRECTORY, storageStatePath);
  if (!isWithinDirectory(FRONTEND_DIRECTORY, storageStateFile)) {
    throw new Error(
      "Feature-data storage state must stay inside the frontend project",
    );
  }
  const state = JSON.parse(
    await readFile(storageStateFile, "utf8"),
  ) as StorageState;
  const cookies = state.cookies ?? [];
  return cookies
    .filter(
      (cookie): cookie is { name: string; value: string; domain?: unknown } =>
        typeof cookie.name === "string" &&
        typeof cookie.value === "string" &&
        (cookie.domain === undefined ||
          cookie.domain === "localhost" ||
          cookie.domain === ".localhost"),
    )
    .map((cookie) => `${cookie.name}=${cookie.value}`)
    .join("; ");
}

function routeReadyPathname(pathname: string, scenario: string): string {
  const url = new URL(pathname, "http://feature-data.local");
  if (url.origin !== "http://feature-data.local") {
    throw new Error("Feature-data readyPathname must be an internal route");
  }
  url.searchParams.set("__feature_data_route_ready", scenario);
  return `${url.pathname}${url.search}`;
}

function requestFrontendRoute(
  port: number,
  pathname: string,
  cookie: string,
  timeoutMs: number,
): Promise<number> {
  return new Promise((resolve, reject) => {
    const request = http.get(
      {
        host: "localhost",
        port,
        path: pathname,
        headers: {
          accept: "text/html",
          "cache-control": "no-cache",
          cookie,
        },
      },
      (response) => {
        response.resume();
        response.once("end", () => resolve(response.statusCode ?? 0));
      },
    );
    request.setTimeout(timeoutMs, () => {
      request.destroy(
        new Error(`Timed out waiting for route-ready response: ${pathname}`),
      );
    });
    request.once("error", reject);
  });
}

async function waitForFrontendRoute(
  frontend: RunningFrontend,
  port: number,
  pathname: string,
  cookie: string,
): Promise<void> {
  const deadline = Date.now() + ROUTE_READY_TIMEOUT_MS;
  let lastError: unknown;
  while (Date.now() < deadline) {
    if (
      frontend.child.exitCode !== null ||
      frontend.child.signalCode !== null
    ) {
      const diagnostic = frontend.diagnostics();
      throw new Error(
        [
          "Feature-data frontend exited before the target route was ready",
          diagnostic,
        ]
          .filter(Boolean)
          .join("\n"),
      );
    }
    try {
      const status = await requestFrontendRoute(
        port,
        pathname,
        cookie,
        Math.max(1, deadline - Date.now()),
      );
      if (status >= 200 && status < 400) return;
      lastError = new Error(
        `Feature-data route-ready probe returned HTTP ${String(status)} for ${pathname}`,
      );
      break;
    } catch (error) {
      lastError = error;
      if (
        !(error instanceof Error) ||
        !("code" in error) ||
        !["ECONNREFUSED", "ECONNRESET"].includes(String(error.code))
      ) {
        break;
      }
    }
    await new Promise<void>((resolve) => setTimeout(resolve, 100));
  }
  const diagnostic = frontend.diagnostics();
  throw new Error(
    [
      lastError instanceof Error
        ? lastError.message
        : `Timed out waiting for feature-data route: ${pathname}`,
      diagnostic,
    ]
      .filter(Boolean)
      .join("\n"),
  );
}

async function stopFrontend(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) {
    return;
  }
  const exited = once(child, "exit");
  child.kill("SIGTERM");
  try {
    await withTimeout(
      exited.then(() => undefined),
      "Timed out stopping feature-data frontend",
    );
  } catch {
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGKILL");
      await once(child, "exit");
    }
    throw new Error("Timed out stopping feature-data frontend after SIGTERM");
  }
}

function scenarioFontResponsesPath(directory: string): string {
  const fontResponsesPath = `${directory}.next-font-google-responses.cjs`;
  if (
    !isWithinDirectory(RUNNER_DIRECTORY, fontResponsesPath) ||
    isWithinDirectory(directory, fontResponsesPath)
  ) {
    throw new Error(
      "Feature-data font responses must be a sibling of the dist directory",
    );
  }
  return fontResponsesPath;
}

async function writeFontResponses(fontResponsesPath: string): Promise<void> {
  await writeFile(
    fontResponsesPath,
    [
      "const response = `@font-face {",
      "  font-family: 'Feature Data E2E';",
      "  src: local('Arial');",
      "}`;",
      "module.exports = new Proxy({}, {",
      "  get: (_target, key) => typeof key === 'string' ? response : undefined,",
      "});",
      "",
    ].join("\n"),
    "utf8",
  );
}

async function cleanupFeatureDataArtifacts(
  directory: string,
  fontResponsesPath: string,
): Promise<void> {
  await Promise.all([
    rm(directory, { force: true, recursive: true }),
    rm(fontResponsesPath, { force: true }),
  ]);
}

export async function startFeatureDataRunner({
  scenario,
  readyPathname,
  storageStatePath,
  heldPathname,
  holdMs = 2_000,
  response,
}: FeatureDataRunnerOptions): Promise<FeatureDataRunner> {
  if (!Number.isSafeInteger(holdMs) || holdMs < 0) {
    throw new Error("Feature-data gate holdMs must be a non-negative integer");
  }
  const directory = scenarioDirectory(scenario);
  const fontResponsesPath = scenarioFontResponsesPath(directory);
  let proxy: RunningProxy | undefined;
  let frontend: RunningFrontend | undefined;

  try {
    await mkdir(directory, { recursive: true });
    await writeFontResponses(fontResponsesPath);
    proxy = await startProxy(heldPathname, holdMs, response);
    const port = await reservePort();
    frontend = startFrontend(directory, fontResponsesPath, port, proxy.url);
    const cookie = await storageStateCookieHeader(storageStatePath);
    await waitForFrontendRoute(
      frontend,
      port,
      routeReadyPathname(readyPathname, scenario),
      cookie,
    );
    proxy.armGate();

    return {
      // auth.setup.ts の host-only localhost cookie をfresh childでも使う。
      baseURL: `http://localhost:${port}`,
      gate: proxy.gate,
      diagnostics: () => frontend?.diagnostics() ?? "",
      dispose: async () => {
        proxy?.gate.release();
        try {
          await Promise.all([
            frontend === undefined ? undefined : stopFrontend(frontend.child),
            proxy === undefined ? undefined : proxy.close(),
          ]);
        } finally {
          await cleanupFeatureDataArtifacts(directory, fontResponsesPath);
        }
      },
    };
  } catch (error) {
    proxy?.gate.release();
    await Promise.allSettled([
      frontend === undefined ? undefined : stopFrontend(frontend.child),
      proxy === undefined ? undefined : proxy.close(),
    ]);
    await cleanupFeatureDataArtifacts(directory, fontResponsesPath);
    throw error;
  }
}
