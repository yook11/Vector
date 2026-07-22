import { betterAuth } from "better-auth";
import { type MemoryDB, memoryAdapter } from "better-auth/adapters/memory";
import { parseSetCookieHeader } from "better-auth/cookies";
import { v7 as uuidv7 } from "uuid";
import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { hashPassword } from "@/lib/auth/password";

const APP_URL = "https://app.example.com";
const PASSWORD = "test-password-123";
const SECRET = "test-better-auth-secret-that-is-at-least-32-characters";

type AuthHandler = {
  handler: (request: Request) => Promise<Response>;
};

type TestMemoryDB = MemoryDB & {
  account: unknown[];
  session: unknown[];
  user: unknown[];
  verification: unknown[];
};

function createDatabase(): TestMemoryDB {
  return {
    account: [],
    session: [],
    user: [],
    verification: [],
  } satisfies TestMemoryDB;
}

function createAuth(
  database: TestMemoryDB,
  disableSignUp: boolean,
): AuthHandler {
  return betterAuth({
    baseURL: APP_URL,
    database: memoryAdapter(database),
    emailAndPassword: {
      enabled: true,
      disableSignUp,
      minPasswordLength: 8,
    },
    user: {
      additionalFields: {
        role: {
          type: "string",
          defaultValue: "user",
          input: false,
        },
      },
    },
    rateLimit: { enabled: false },
    secret: SECRET,
    trustedOrigins: [APP_URL],
  });
}

function signUpRequest(
  auth: AuthHandler,
  body: unknown,
  origin = APP_URL,
): Promise<Response> {
  return auth.handler(
    new Request(`${APP_URL}/api/auth/sign-up/email`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin,
      },
      body: JSON.stringify(body),
    }),
  );
}

function signInRequest(auth: AuthHandler, email: string): Promise<Response> {
  return auth.handler(
    new Request(`${APP_URL}/api/auth/sign-in/email`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin: APP_URL,
      },
      body: JSON.stringify({ email, password: PASSWORD }),
    }),
  );
}

function adminRequest(auth: AuthHandler, path: string): Promise<Response> {
  return auth.handler(
    new Request(`${APP_URL}/api/auth/admin/${path}`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin: APP_URL,
      },
      body: JSON.stringify({}),
    }),
  );
}

function recordCounts(database: TestMemoryDB) {
  return {
    account: database.account.length,
    session: database.session.length,
    user: database.user.length,
  };
}

function cookieHeaderFrom(response: Response): string {
  const setCookie = response.headers.get("set-cookie");
  if (setCookie === null) {
    throw new Error("Expected Better Auth sign-up to set a session cookie");
  }

  const cookies = parseSetCookieHeader(setCookie);
  if (cookies.size === 0) {
    throw new Error("Expected at least one parseable session cookie");
  }

  return Array.from(
    cookies,
    ([name, attributes]) => `${name}=${encodeURIComponent(attributes.value)}`,
  ).join("; ");
}

describe("public signup disabled boundary", () => {
  it("does not expose Better Auth admin user mutation endpoints or change records", async () => {
    const database = createDatabase();
    const auth = createAuth(database, true);
    const before = recordCounts(database);

    for (const path of ["create-user", "set-role"]) {
      const response = await adminRequest(auth, path);

      expect(response.status).toBe(404);
      expect(recordCounts(database)).toEqual(before);
    }
  });

  it("rejects a schema-valid trusted-origin signup with the documented error before creating records", async () => {
    const database = createDatabase();
    const auth = createAuth(database, true);

    const response = await signUpRequest(auth, {
      email: "new-user@example.com",
      name: "New User",
      password: PASSWORD,
    });

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toMatchObject({
      code: "EMAIL_PASSWORD_SIGN_UP_DISABLED",
    });
    expect(recordCounts(database)).toEqual({ account: 0, session: 0, user: 0 });
  });

  it("does not create records when signup body schema validation rejects first", async () => {
    const database = createDatabase();
    const auth = createAuth(database, true);

    const response = await signUpRequest(auth, {
      email: "new-user@example.com",
      name: 42,
      password: PASSWORD,
    });

    expect(response.ok).toBe(false);
    expect(recordCounts(database)).toEqual({ account: 0, session: 0, user: 0 });
  });

  it("does not create records when origin validation rejects first", async () => {
    const database = createDatabase();
    const auth = createAuth(database, true);

    const response = await signUpRequest(
      auth,
      {
        email: "new-user@example.com",
        name: "New User",
        password: PASSWORD,
      },
      "https://untrusted.example.com",
    );

    expect(response.ok).toBe(false);
    expect(recordCounts(database)).toEqual({ account: 0, session: 0, user: 0 });
  });

  it("keeps email/password sign-in available for an existing user", async () => {
    const database = createDatabase();
    const signupEnabledAuth = createAuth(database, false);

    const seedResponse = await signUpRequest(signupEnabledAuth, {
      email: "existing@example.com",
      name: "Existing User",
      password: PASSWORD,
    });

    expect(seedResponse.status).toBe(200);
    expect(recordCounts(database)).toEqual({ account: 1, session: 1, user: 1 });

    const signupDisabledAuth = createAuth(database, true);
    const signInResponse = await signInRequest(
      signupDisabledAuth,
      "existing@example.com",
    );

    expect(signInResponse.status).toBe(200);
    expect(recordCounts(database)).toEqual({ account: 1, session: 2, user: 1 });
  });

  it("signs in a provisioning-shaped credential hashed by the production password export", async () => {
    const database = createDatabase();
    const auth = createAuth(database, true);
    const userId = uuidv7();
    const accountId = uuidv7();
    const now = new Date("2026-07-22T00:00:00.000Z");
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const consoleLog = vi.spyOn(console, "log").mockImplementation(() => {});

    try {
      const passwordHash = await hashPassword(PASSWORD);
      database.user.push({
        id: userId,
        name: "Provisioned User",
        email: "provisioned@example.com",
        emailVerified: false,
        createdAt: now,
        updatedAt: now,
        role: "user",
      });
      database.account.push({
        id: accountId,
        accountId: userId,
        providerId: "credential",
        userId,
        password: passwordHash,
        createdAt: now,
        updatedAt: now,
      });
      const before = recordCounts(database);

      const response = await signInRequest(auth, "provisioned@example.com");

      expect(response.status).toBe(200);
      expect(recordCounts(database)).toEqual({
        account: before.account,
        session: before.session + 1,
        user: before.user,
      });
      expect(consoleError).not.toHaveBeenCalled();
      expect(consoleWarn).not.toHaveBeenCalled();
      expect(consoleLog).not.toHaveBeenCalled();
    } finally {
      consoleError.mockRestore();
      consoleWarn.mockRestore();
      consoleLog.mockRestore();
    }
  });

  it("accepts a session issued before signup was disabled", async () => {
    const database = createDatabase();
    const signupEnabledAuth = createAuth(database, false);

    const signupResponse = await signUpRequest(signupEnabledAuth, {
      email: "existing@example.com",
      name: "Existing User",
      password: PASSWORD,
    });

    expect(signupResponse.status).toBe(200);
    expect(recordCounts(database)).toEqual({ account: 1, session: 1, user: 1 });

    const signupDisabledAuth = createAuth(database, true);
    const response = await signupDisabledAuth.handler(
      new Request(`${APP_URL}/api/auth/get-session`, {
        headers: {
          cookie: cookieHeaderFrom(signupResponse),
          origin: APP_URL,
        },
      }),
    );

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({
      user: { email: "existing@example.com" },
    });
    expect(recordCounts(database)).toEqual({ account: 1, session: 1, user: 1 });
  });
});
