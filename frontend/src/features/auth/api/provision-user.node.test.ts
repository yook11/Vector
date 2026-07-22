import { existsSync } from "node:fs";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  class CreateUserWithCredentialError extends Error {
    readonly code: "duplicate-email" | "internal";

    constructor(code: "duplicate-email" | "internal") {
      super(
        code === "duplicate-email"
          ? "A user with this email already exists."
          : "Unable to provision user.",
      );
      this.name = "CreateUserWithCredentialError";
      this.code = code;
    }
  }

  return {
    isRedirectError: vi.fn(),
    createUserWithCredential: vi.fn(),
    CreateUserWithCredentialError,
    requireAdminForAction: vi.fn(),
  };
});

vi.mock("server-only", () => ({}));
vi.mock("@/lib/auth/guards", () => ({
  requireAdminForAction: mocks.requireAdminForAction,
}));
vi.mock("@/lib/utils/redirect-error", () => ({
  isRedirectError: mocks.isRedirectError,
}));
vi.mock("../server/create-user-with-credential", () => ({
  createUserWithCredential: mocks.createUserWithCredential,
  CreateUserWithCredentialError: mocks.CreateUserWithCredentialError,
}));

const ACTION_URL = new URL("./provision-user.ts", import.meta.url);
const ACTION_MODULE_PATH = "./provision-user";
const PASSWORD = "plain-password";
const PASSWORD_HASH = "hash-output-only";
const EMAIL = "new-user@example.com";

type ProvisionUser = (...args: unknown[]) => Promise<unknown>;

function formData(entries: Record<string, string>): FormData {
  const data = new FormData();
  for (const [key, value] of Object.entries(entries)) {
    data.set(key, value);
  }
  return data;
}

function validFormData(): FormData {
  return formData({
    name: "山田 太郎",
    email: EMAIL,
    password: PASSWORD,
  });
}

async function loadProvisionUser(): Promise<ProvisionUser> {
  expect(existsSync(ACTION_URL)).toBe(true);
  const actionModule: object = await import(
    /* @vite-ignore */ ACTION_MODULE_PATH
  );
  const provisionUser = Reflect.get(actionModule, "provisionUser");
  expect(provisionUser).toEqual(expect.any(Function));
  return provisionUser as ProvisionUser;
}

function invokeAction(action: ProvisionUser, data: FormData): Promise<unknown> {
  return action.length >= 2 ? action({ status: "idle" }, data) : action(data);
}

function errorWithCode(code: string): Error & { code: string } {
  return Object.assign(new Error(`service failure ${EMAIL} ${PASSWORD_HASH}`), {
    code,
  });
}

function responseText(response: unknown): string {
  return JSON.stringify(response) ?? "";
}

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  mocks.requireAdminForAction.mockResolvedValue(undefined);
  mocks.isRedirectError.mockReturnValue(false);
  mocks.createUserWithCredential.mockResolvedValue(undefined);
});

describe("provisionUser", () => {
  it("rethrows the same authentication redirect before provisioning", async () => {
    const redirectError = Object.assign(new Error("NEXT_REDIRECT"), {
      digest: "NEXT_REDIRECT;replace;/auth/login;307;",
    });
    mocks.requireAdminForAction.mockRejectedValueOnce(redirectError);
    mocks.isRedirectError.mockImplementation(
      (error: unknown) => error === redirectError,
    );
    const action = await loadProvisionUser();

    await expect(invokeAction(action, validFormData())).rejects.toBe(
      redirectError,
    );

    expect(mocks.requireAdminForAction).toHaveBeenCalledOnce();
    expect(mocks.createUserWithCredential).not.toHaveBeenCalled();
  });

  it("maps Forbidden to a safe Japanese authorization state without provisioning", async () => {
    mocks.requireAdminForAction.mockRejectedValueOnce(new Error("Forbidden"));
    const action = await loadProvisionUser();

    const response = await invokeAction(action, validFormData());

    expect(response).toMatchObject({
      status: "error",
      formError: expect.stringMatching(/権限/),
    });
    expect(responseText(response)).not.toContain("Forbidden");
    expect(mocks.createUserWithCredential).not.toHaveBeenCalled();
  });

  it("returns Japanese field errors for invalid input without calling the service", async () => {
    const action = await loadProvisionUser();
    const response = await invokeAction(
      action,
      formData({
        name: "",
        email: "not-an-email",
        password: "short",
        role: "admin",
        data: '{"role":"admin"}',
        id: "attacker-id",
        providerId: "credential",
      }),
    );

    expect(response).toMatchObject({
      status: "error",
      fieldErrors: {
        name: expect.stringMatching(/[ぁ-んァ-ン一-龯]/),
        email: expect.stringMatching(/[ぁ-んァ-ン一-龯]/),
        password: expect.stringMatching(/[ぁ-んァ-ン一-龯]/),
      },
    });
    expect(mocks.createUserWithCredential).not.toHaveBeenCalled();
  });

  it("rejects injected fields after authorization without calling the service", async () => {
    const action = await loadProvisionUser();

    for (const [field, value] of Object.entries({
      role: "admin",
      data: '{"role":"admin"}',
      id: "attacker-id",
      providerId: "credential",
    })) {
      const data = validFormData();
      data.set(field, value);
      const response = await invokeAction(action, data);

      expect(response).toMatchObject({ status: "error" });
    }

    expect(mocks.requireAdminForAction).toHaveBeenCalledTimes(4);
    expect(mocks.createUserWithCredential).not.toHaveBeenCalled();
  });

  it("passes normalized input to the service and returns an email-only success state", async () => {
    const guardCompleted = vi.fn();
    mocks.requireAdminForAction.mockImplementationOnce(async () => {
      await Promise.resolve();
      guardCompleted();
    });
    mocks.createUserWithCredential.mockImplementationOnce(async () => {
      expect(guardCompleted).toHaveBeenCalledOnce();
    });
    const action = await loadProvisionUser();

    const response = await invokeAction(
      action,
      formData({
        name: "  山田 太郎  ",
        email: "  NEW-USER@EXAMPLE.COM  ",
        password: PASSWORD,
      }),
    );

    expect(mocks.createUserWithCredential).toHaveBeenCalledOnce();
    const guardCallOrder =
      mocks.requireAdminForAction.mock.invocationCallOrder[0];
    const serviceCallOrder =
      mocks.createUserWithCredential.mock.invocationCallOrder[0];
    if (guardCallOrder === undefined || serviceCallOrder === undefined) {
      throw new Error(
        "管理者ガードとプロビジョニングサービスの呼出が必要です。",
      );
    }
    expect(guardCallOrder).toBeLessThan(serviceCallOrder);
    expect(mocks.createUserWithCredential).toHaveBeenCalledWith({
      name: "山田 太郎",
      email: EMAIL,
      password: PASSWORD,
    });
    expect(response).toMatchObject({ status: "success", email: EMAIL });
    const text = responseText(response);
    for (const secret of [PASSWORD, PASSWORD_HASH]) {
      expect(text).not.toContain(secret);
    }
    expect(text).not.toContain("role");
    expect(text).not.toContain("session");
  });

  it("ignores only Next Server Action transport metadata before strict validation", async () => {
    const action = await loadProvisionUser();

    const response = await invokeAction(
      action,
      formData({
        name: "  山田 太郎  ",
        email: "  NEW-USER@EXAMPLE.COM  ",
        password: PASSWORD,
        $ACTION_ID_provision_user: "action-id",
        $ACTION_REF_0: "action-reference",
        $ACTION_role: "admin",
      }),
    );

    expect(mocks.createUserWithCredential).toHaveBeenCalledOnce();
    expect(mocks.createUserWithCredential).toHaveBeenCalledWith({
      name: "山田 太郎",
      email: EMAIL,
      password: PASSWORD,
    });
    expect(response).toEqual({ status: "success", email: EMAIL });
  });

  it.each([
    [
      "dedicated duplicate-email error",
      new mocks.CreateUserWithCredentialError("duplicate-email"),
      "このメールアドレスは登録済みです。",
    ],
    [
      "arbitrary duplicate-email code",
      errorWithCode("duplicate-email"),
      "ユーザーの登録に失敗しました。",
    ],
    [
      "dedicated internal error",
      new mocks.CreateUserWithCredentialError("internal"),
      "ユーザーの登録に失敗しました。",
    ],
    [
      "unknown error code",
      errorWithCode("unknown"),
      "ユーザーの登録に失敗しました。",
    ],
  ])("maps %s to a redacted Japanese error state", async (_label, error, message) => {
    mocks.createUserWithCredential.mockRejectedValueOnce(error);
    const action = await loadProvisionUser();
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const consoleLog = vi.spyOn(console, "log").mockImplementation(() => {});

    try {
      const response = await invokeAction(action, validFormData());

      expect(response).toMatchObject({ status: "error", formError: message });
      const text = responseText(response);
      for (const secret of [PASSWORD, PASSWORD_HASH, EMAIL]) {
        expect(text).not.toContain(secret);
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
