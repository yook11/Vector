import { beforeEach, describe, expect, it, vi } from "vitest";

type AuthOptions = {
  emailAndPassword?: unknown;
};

const mocks = vi.hoisted(() => {
  const poolOn = vi.fn();

  return {
    betterAuth: vi.fn((options: AuthOptions) => ({ options })),
    Pool: class {
      on = poolOn;
    },
    requireEnv: vi.fn((name: string) => {
      const values: Record<string, string> = {
        AUTH_DATABASE_URL: "postgresql://user:password@localhost:5432/auth",
        BETTER_AUTH_URL: "https://app.example.com",
      };
      return values[name] ?? "test-value";
    }),
  };
});

vi.mock("server-only", () => ({}));
vi.mock("better-auth", () => ({ betterAuth: mocks.betterAuth }));
vi.mock("pg", () => ({ Pool: mocks.Pool }));
vi.mock("@/lib/env", () => ({ requireEnv: mocks.requireEnv }));

function capturedOptions(): AuthOptions {
  const options = mocks.betterAuth.mock.calls.at(-1)?.[0];
  if (options === undefined) {
    throw new Error("Expected auth module to initialize Better Auth");
  }
  return options as AuthOptions;
}

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
});

describe("Better Auth public signup configuration", () => {
  it("runtime auth disables public signup while retaining email/password sign-in", async () => {
    await import("./auth");

    expect(capturedOptions().emailAndPassword).toEqual({
      enabled: true,
      disableSignUp: true,
      minPasswordLength: 8,
    });
  });

  it("CLI auth uses the same email/password signup mode as runtime auth", async () => {
    await import("./auth.cli");

    expect(capturedOptions().emailAndPassword).toEqual({
      enabled: true,
      disableSignUp: true,
      minPasswordLength: 8,
    });
  });
});
