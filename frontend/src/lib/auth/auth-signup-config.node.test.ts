import { beforeEach, describe, expect, it, vi } from "vitest";

type AuthOptions = {
  emailAndPassword?: {
    enabled: boolean;
    disableSignUp: boolean;
    minPasswordLength: number;
    maxPasswordLength: number;
  };
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
  it("runtime auth keeps signup disabled with the shared standard password policy", async () => {
    await import("./auth");
    const options = capturedOptions();

    expect(options.emailAndPassword).toEqual({
      enabled: true,
      disableSignUp: true,
      minPasswordLength: 8,
      maxPasswordLength: 128,
    });
    expect(options).not.toHaveProperty("plugins");
  });

  it("CLI auth keeps the same signup and standard password policy as runtime", async () => {
    await import("./auth.cli");
    const options = capturedOptions();

    expect(options.emailAndPassword).toEqual({
      enabled: true,
      disableSignUp: true,
      minPasswordLength: 8,
      maxPasswordLength: 128,
    });
    expect(options).not.toHaveProperty("plugins");
  });
});
