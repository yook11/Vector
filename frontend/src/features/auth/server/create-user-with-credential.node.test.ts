import { existsSync } from "node:fs";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  authPoolConnect: vi.fn(),
  hashPassword: vi.fn(),
  uuidV7: vi.fn(),
}));

vi.mock("server-only", () => ({}));
vi.mock("@/lib/auth/auth-db", () => ({
  authPool: { connect: mocks.authPoolConnect },
}));
vi.mock("@/lib/auth/password", () => ({ hashPassword: mocks.hashPassword }));
vi.mock("uuid", () => ({ v7: mocks.uuidV7 }));

const SERVICE_URL = new URL(
  "./create-user-with-credential.ts",
  import.meta.url,
);
const SERVICE_MODULE_PATH = "./create-user-with-credential";
const INPUT = {
  name: "山田 太郎",
  email: "new-user@example.com",
  password: "operator-password",
};
const USER_ID = "019f7eea-1030-7f00-8e89-d274f4f1f0b8";
const ACCOUNT_ID = "019f7eea-1030-7f01-8e89-d274f4f1f0b9";
const PASSWORD_HASH = "hash-output-only";
const CONNECTION_URL =
  "postgresql://vector_auth:connection-secret@auth-db:5432/vector";

type CreateUserWithCredential = (input: typeof INPUT) => Promise<unknown>;
type Query = (sql: string, parameters?: readonly unknown[]) => Promise<unknown>;
type Release = (destroy?: boolean) => void;
type PublicProvisionError = Error & { code?: unknown; cause?: unknown };

async function loadCreateUserWithCredential(): Promise<CreateUserWithCredential> {
  expect(existsSync(SERVICE_URL)).toBe(true);
  const serviceModule: object = await import(
    /* @vite-ignore */ SERVICE_MODULE_PATH
  );
  const createUserWithCredential = Reflect.get(
    serviceModule,
    "createUserWithCredential",
  );
  expect(createUserWithCredential).toEqual(expect.any(Function));
  return createUserWithCredential as CreateUserWithCredential;
}

function configuredClient() {
  const query = vi.fn<Query>().mockResolvedValue(undefined);
  const release = vi.fn<Release>();
  mocks.authPoolConnect.mockResolvedValue({ query, release });
  return { query, release };
}

function queryStatements(query: ReturnType<typeof vi.fn<Query>>): string[] {
  return query.mock.calls.map(([sql]) => sql);
}

function queryStatement(
  query: ReturnType<typeof vi.fn<Query>>,
  index: number,
): string {
  const statement = query.mock.calls[index]?.[0];
  expect(typeof statement).toBe("string");
  if (typeof statement !== "string") {
    throw new Error(`Expected SQL statement at query index ${index}`);
  }
  return statement;
}

function queryParameters(
  query: ReturnType<typeof vi.fn<Query>>,
  index: number,
): readonly unknown[] {
  const parameters = query.mock.calls[index]?.[1];
  expect(parameters).toBeDefined();
  return parameters ?? [];
}

async function rejectedError(operation: Promise<unknown>): Promise<unknown> {
  try {
    await operation;
  } catch (error) {
    return error;
  }
  throw new Error("Expected createUserWithCredential to reject");
}

function rawDatabaseError(code?: string, constraint?: string): Error {
  return Object.assign(
    new Error(
      `database error ${CONNECTION_URL} ${INPUT.email} ${INPUT.password} ${PASSWORD_HASH}`,
    ),
    {
      code,
      constraint,
      detail: `Key (email)=(${INPUT.email}) conflicts with ${PASSWORD_HASH}`,
    },
  );
}

function expectSafeProvisionError(
  error: unknown,
  code: string,
  additionalSecrets: readonly string[] = [],
): void {
  expect(error).toBeInstanceOf(Error);
  expect(error).toMatchObject({ code });
  const publicError = error as PublicProvisionError;
  const exposed = [
    publicError.message,
    ...Object.values(publicError).map((value) => JSON.stringify(value) ?? ""),
    JSON.stringify(publicError.cause) ?? "",
  ].join("\n");
  for (const secret of [
    CONNECTION_URL,
    INPUT.email,
    INPUT.password,
    PASSWORD_HASH,
    ...additionalSecrets,
  ]) {
    expect(exposed).not.toContain(secret);
  }
}

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  mocks.hashPassword.mockResolvedValue(PASSWORD_HASH);
  mocks.uuidV7.mockReturnValueOnce(USER_ID).mockReturnValueOnce(ACCOUNT_ID);
});

describe("createUserWithCredential", () => {
  it("hashes before connecting and creates one user and credential account transaction", async () => {
    const { query, release } = configuredClient();
    mocks.hashPassword.mockImplementationOnce(async (password: unknown) => {
      expect(password).toBe(INPUT.password);
      expect(mocks.authPoolConnect).not.toHaveBeenCalled();
      expect(query).not.toHaveBeenCalled();
      return PASSWORD_HASH;
    });
    const createUserWithCredential = await loadCreateUserWithCredential();

    await createUserWithCredential(INPUT);

    expect(mocks.uuidV7).toHaveBeenNthCalledWith(1);
    expect(mocks.uuidV7).toHaveBeenNthCalledWith(2);
    expect(query.mock.calls).toHaveLength(4);
    const beginStatement = queryStatement(query, 0);
    const userStatement = queryStatement(query, 1);
    const accountStatement = queryStatement(query, 2);
    const commitStatement = queryStatement(query, 3);
    const statements = [
      beginStatement,
      userStatement,
      accountStatement,
      commitStatement,
    ];
    expect(beginStatement).toMatch(/^BEGIN$/i);
    expect(userStatement).toMatch(/INSERT\s+INTO\s+auth\."user"/i);
    expect(accountStatement).toMatch(/INSERT\s+INTO\s+auth\.account/i);
    expect(commitStatement).toMatch(/^COMMIT$/i);
    expect(userStatement).toMatch(/'user'/i);
    expect(accountStatement).toMatch(/'credential'/i);

    const userParameters = queryParameters(query, 1);
    const accountParameters = queryParameters(query, 2);
    expect(
      /\bfalse\b/i.test(userStatement) || userParameters.includes(false),
    ).toBe(true);
    expect(userParameters).toEqual(
      expect.arrayContaining([USER_ID, INPUT.name, INPUT.email]),
    );
    expect(accountParameters).toEqual(
      expect.arrayContaining([ACCOUNT_ID, USER_ID, PASSWORD_HASH]),
    );
    expect(accountParameters.filter((value) => value === USER_ID)).toHaveLength(
      2,
    );
    const userTimestamp = userParameters.find((value) => value instanceof Date);
    const accountTimestamp = accountParameters.find(
      (value) => value instanceof Date,
    );
    expect(userTimestamp).toEqual(accountTimestamp);

    const sqlText = statements.join("\n");
    for (const value of [
      INPUT.name,
      INPUT.email,
      INPUT.password,
      PASSWORD_HASH,
    ]) {
      expect(sqlText).not.toContain(value);
    }
    expect(sqlText).not.toMatch(/uuidv7\s*\(/i);
    const inserts = statements.filter((statement) =>
      /INSERT\s+INTO/i.test(statement),
    );
    expect(inserts).toHaveLength(2);
    expect(
      inserts.every((statement) => /auth\.(?:"user"|account)/i.test(statement)),
    ).toBe(true);
    expect(release).toHaveBeenCalledOnce();
  });

  it("does not acquire a client when hashing fails", async () => {
    const { query } = configuredClient();
    mocks.hashPassword.mockRejectedValueOnce(new Error("hash unavailable"));
    const createUserWithCredential = await loadCreateUserWithCredential();

    await expect(createUserWithCredential(INPUT)).rejects.toBeInstanceOf(Error);

    expect(mocks.authPoolConnect).not.toHaveBeenCalled();
    expect(query).not.toHaveBeenCalled();
  });

  it.each([
    ["user insert", 1],
    ["account insert", 2],
    ["commit", 3],
  ])("rolls back and releases when %s fails", async (_phase, failureIndex) => {
    const { query, release } = configuredClient();
    for (let index = 0; index < failureIndex; index += 1) {
      query.mockResolvedValueOnce(undefined);
    }
    query.mockRejectedValueOnce(new Error("database unavailable"));
    const createUserWithCredential = await loadCreateUserWithCredential();

    await expect(createUserWithCredential(INPUT)).rejects.toBeInstanceOf(Error);

    const statements = queryStatements(query);
    expect(statements).toContain("ROLLBACK");
    if (failureIndex < 3) {
      expect(statements).not.toContain("COMMIT");
    }
    expect(release).toHaveBeenCalledOnce();
  });

  it("destroys the client when rollback fails without exposing either database error", async () => {
    const { query, release } = configuredClient();
    const primaryError = rawDatabaseError("40001");
    const rollbackSecret = "rollback-connection-secret";
    const rollbackError = new Error(`rollback failed ${rollbackSecret}`);
    query
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(primaryError)
      .mockRejectedValueOnce(rollbackError);
    const createUserWithCredential = await loadCreateUserWithCredential();

    const error = await rejectedError(createUserWithCredential(INPUT));

    expectSafeProvisionError(error, "internal", [rollbackSecret]);
    expect(error).not.toBe(primaryError);
    expect(error).not.toBe(rollbackError);
    expect(queryStatements(query)).toContain("ROLLBACK");
    expect(release).toHaveBeenCalledOnce();
    expect(release).toHaveBeenCalledWith(true);
  });

  it("classifies only the user email unique violation as duplicate-email", async () => {
    const { query, release } = configuredClient();
    const databaseError = rawDatabaseError("23505", "user_email_key");
    query.mockResolvedValueOnce(undefined).mockRejectedValueOnce(databaseError);
    const createUserWithCredential = await loadCreateUserWithCredential();
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const consoleLog = vi.spyOn(console, "log").mockImplementation(() => {});

    try {
      const error = await rejectedError(createUserWithCredential(INPUT));

      expectSafeProvisionError(error, "duplicate-email");
      expect(error).not.toBe(databaseError);
      expect(queryStatements(query)).toContain("ROLLBACK");
      expect(release).toHaveBeenCalledOnce();
      expect(consoleError).not.toHaveBeenCalled();
      expect(consoleWarn).not.toHaveBeenCalled();
      expect(consoleLog).not.toHaveBeenCalled();
    } finally {
      consoleError.mockRestore();
      consoleWarn.mockRestore();
      consoleLog.mockRestore();
    }
  });

  it.each([
    [
      "a different unique constraint",
      "user",
      rawDatabaseError("23505", "other_key"),
    ],
    [
      "a unique violation without a constraint",
      "user",
      rawDatabaseError("23505"),
    ],
    ["another database error", "user", rawDatabaseError("40001")],
    ["a connection failure", "connect", rawDatabaseError()],
    ["a hash failure", "hash", rawDatabaseError()],
    ["a commit failure", "commit", rawDatabaseError()],
  ])("classifies %s as internal without exposing secrets", async (_label, phase, rawError) => {
    const { query, release } = configuredClient();
    if (phase === "hash") {
      mocks.hashPassword.mockRejectedValueOnce(rawError);
    } else if (phase === "connect") {
      mocks.authPoolConnect.mockRejectedValueOnce(rawError);
    } else if (phase === "commit") {
      query
        .mockResolvedValueOnce(undefined)
        .mockResolvedValueOnce(undefined)
        .mockResolvedValueOnce(undefined)
        .mockRejectedValueOnce(rawError);
    } else {
      query.mockResolvedValueOnce(undefined).mockRejectedValueOnce(rawError);
    }
    const createUserWithCredential = await loadCreateUserWithCredential();
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const consoleLog = vi.spyOn(console, "log").mockImplementation(() => {});

    try {
      const error = await rejectedError(createUserWithCredential(INPUT));

      expectSafeProvisionError(error, "internal");
      expect(error).not.toBe(rawError);
      if (phase === "user" || phase === "commit") {
        expect(queryStatements(query)).toContain("ROLLBACK");
        expect(release).toHaveBeenCalledOnce();
      } else if (phase === "hash") {
        expect(mocks.authPoolConnect).not.toHaveBeenCalled();
      } else {
        expect(mocks.authPoolConnect).toHaveBeenCalledOnce();
      }
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
