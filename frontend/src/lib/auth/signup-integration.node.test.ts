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

type StoredUser = {
  email: string;
  emailVerified: boolean;
  name: string;
  role: string;
};

type TestMemoryDB = MemoryDB & {
  account: unknown[];
  session: unknown[];
  user: StoredUser[];
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

function createAuth(database: TestMemoryDB): AuthHandler {
  return betterAuth({
    baseURL: APP_URL,
    database: memoryAdapter(database),
    emailAndPassword: {
      enabled: true,
      disableSignUp: false,
      minPasswordLength: 8,
      maxPasswordLength: 128,
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
    // Better Auth は NODE_ENV=test で origin 検査を既定で外すため、本番と同じく有効にする。
    advanced: { disableOriginCheck: false },
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

describe("public signup boundary", () => {
  it("creates a user, credential account and signed-in session", async () => {
    const database = createDatabase();
    const auth = createAuth(database);

    const signUpResponse = await signUpRequest(auth, {
      email: "new-user@example.com",
      name: "",
      password: PASSWORD,
    });

    expect(signUpResponse.status).toBe(200);
    expect(recordCounts(database)).toEqual({ account: 1, session: 1, user: 1 });
    expect(database.user[0]).toMatchObject({
      email: "new-user@example.com",
      emailVerified: false,
      name: "",
      role: "user",
    });

    const sessionResponse = await auth.handler(
      new Request(`${APP_URL}/api/auth/get-session`, {
        headers: {
          cookie: cookieHeaderFrom(signUpResponse),
          origin: APP_URL,
        },
      }),
    );

    expect(sessionResponse.status).toBe(200);
    await expect(sessionResponse.json()).resolves.toMatchObject({
      user: { email: "new-user@example.com", role: "user" },
    });
  });

  it("ignores role and emailVerified supplied in the signup body", async () => {
    const database = createDatabase();
    const auth = createAuth(database);

    const response = await signUpRequest(auth, {
      email: "new-user@example.com",
      name: "",
      password: PASSWORD,
      role: "admin",
      emailVerified: true,
    });

    expect(response.status).toBe(200);
    expect(database.user[0]).toMatchObject({
      emailVerified: false,
      role: "user",
    });
  });

  it("stores the email in lowercase", async () => {
    const database = createDatabase();
    const auth = createAuth(database);

    const response = await signUpRequest(auth, {
      email: "Mixed.Case@Example.COM",
      name: "",
      password: PASSWORD,
    });

    expect(response.status).toBe(200);
    expect(database.user[0]).toMatchObject({
      email: "mixed.case@example.com",
    });
  });

  it("rejects an already registered email regardless of case without creating records", async () => {
    const database = createDatabase();
    const auth = createAuth(database);
    await signUpRequest(auth, {
      email: "new-user@example.com",
      name: "",
      password: PASSWORD,
    });
    const before = recordCounts(database);

    const response = await signUpRequest(auth, {
      email: "NEW-USER@example.com",
      name: "",
      password: PASSWORD,
    });

    expect(response.status).toBe(422);
    await expect(response.json()).resolves.toMatchObject({
      code: "USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL",
    });
    expect(recordCounts(database)).toEqual(before);
  });

  it("does not create records when signup body schema validation rejects first", async () => {
    const database = createDatabase();
    const auth = createAuth(database);

    const response = await signUpRequest(auth, {
      email: "new-user@example.com",
      name: 42,
      password: PASSWORD,
    });

    expect(response.ok).toBe(false);
    expect(recordCounts(database)).toEqual({ account: 0, session: 0, user: 0 });
  });

  it("rejects an untrusted origin without creating records", async () => {
    const database = createDatabase();
    const auth = createAuth(database);

    const response = await signUpRequest(
      auth,
      {
        email: "new-user@example.com",
        name: "",
        password: PASSWORD,
      },
      "https://untrusted.example.com",
    );

    expect(response.status).toBe(403);
    await expect(response.json()).resolves.toMatchObject({
      code: "INVALID_ORIGIN",
    });
    expect(recordCounts(database)).toEqual({ account: 0, session: 0, user: 0 });
  });

  it("does not expose Better Auth admin user mutation endpoints or change records", async () => {
    const database = createDatabase();
    const auth = createAuth(database);
    const before = recordCounts(database);

    for (const path of ["create-user", "set-role"]) {
      const response = await adminRequest(auth, path);

      expect(response.status).toBe(404);
      expect(recordCounts(database)).toEqual(before);
    }
  });

  it("keeps email/password sign-in available for a registered user", async () => {
    const database = createDatabase();
    const auth = createAuth(database);
    await signUpRequest(auth, {
      email: "existing@example.com",
      name: "",
      password: PASSWORD,
    });

    const response = await signInRequest(auth, "existing@example.com");

    expect(response.status).toBe(200);
    expect(recordCounts(database)).toEqual({ account: 1, session: 2, user: 1 });
  });

  it("signs in a provisioning-shaped credential hashed by the production password export", async () => {
    const database = createDatabase();
    const auth = createAuth(database);
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
      } as StoredUser);
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
});
