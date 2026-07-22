import { existsSync } from "node:fs";
import type { ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  ProvisionUserForm: vi.fn(() => "provision-user-form"),
  requireAdmin: vi.fn(),
}));

vi.mock("@/features/auth", () => ({
  ProvisionUserForm: mocks.ProvisionUserForm,
}));
vi.mock("@/lib/auth/guards", () => ({ requireAdmin: mocks.requireAdmin }));

const PAGE_URL = new URL("./page.tsx", import.meta.url);
const PAGE_MODULE_PATH = "./page";

type UsersNewPage = () => ReactElement | Promise<ReactElement>;

interface UsersNewPageModule {
  default: UsersNewPage;
  metadata: { title?: unknown };
}

async function loadUsersNewPage(): Promise<UsersNewPageModule> {
  expect(existsSync(PAGE_URL)).toBe(true);
  const pageModule: object = await import(/* @vite-ignore */ PAGE_MODULE_PATH);
  const page = Reflect.get(pageModule, "default");
  expect(page).toEqual(expect.any(Function));
  return {
    default: page as UsersNewPage,
    metadata: Reflect.get(
      pageModule,
      "metadata",
    ) as UsersNewPageModule["metadata"],
  };
}

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  mocks.requireAdmin.mockResolvedValue(undefined);
});

describe("/admin/users/new", () => {
  it("admin guard の失敗をそのまま伝播し、Form を描画しない", async () => {
    const guardError = new Error("Forbidden");
    mocks.requireAdmin.mockRejectedValueOnce(guardError);
    const { default: UsersNewPage } = await loadUsersNewPage();

    await expect(UsersNewPage()).rejects.toBe(guardError);

    expect(mocks.requireAdmin).toHaveBeenCalledOnce();
    expect(mocks.ProvisionUserForm).not.toHaveBeenCalled();
  });

  it("admin に日本語の登録画面と Form を表示し、guard を先に await する", async () => {
    const { default: UsersNewPage, metadata } = await loadUsersNewPage();

    const markup = renderToStaticMarkup(await UsersNewPage());

    expect(markup).toMatch(/ユーザー.*登録|登録.*ユーザー/);
    expect(markup).toMatch(/デモ|一般ユーザー/);
    expect(mocks.ProvisionUserForm).toHaveBeenCalledOnce();
    const guardOrder = mocks.requireAdmin.mock.invocationCallOrder[0];
    const formOrder = mocks.ProvisionUserForm.mock.invocationCallOrder[0];
    if (guardOrder === undefined || formOrder === undefined) {
      throw new Error("guard と Form の呼出が必要です。");
    }
    expect(guardOrder).toBeLessThan(formOrder);
    expect(metadata.title).toEqual(
      expect.stringMatching(/ユーザー|ユーザ|登録|新規/),
    );
  });
});
