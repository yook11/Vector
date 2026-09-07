import { describe, expect, it, vi } from "vitest";

vi.mock("@/features/auth", () => ({ LoginForm: () => null }));

import LoginPage from "./page";

describe("ログインページの復帰先", () => {
  it.each([
    ["/news/../watchlist?_rsc=a&page=2#saved", "/watchlist?page=2#saved"],
    [["/watchlist", "/research"], "/"],
    ["https://evil.test", "/"],
    ["/%61uth/login", "/"],
    [undefined, "/"],
  ] as const)("%j を検証してフォームへ渡す", async (callbackUrl, expected) => {
    const page = await LoginPage({
      searchParams: Promise.resolve(
        callbackUrl === undefined
          ? {}
          : {
              callbackUrl:
                typeof callbackUrl === "string"
                  ? callbackUrl
                  : [...callbackUrl],
            },
      ),
    });
    expect(page.props.children.props.returnTo).toBe(expected);
  });
});
