import { describe, expect, it, vi } from "vitest";

vi.mock("@/features/auth", () => ({ LoginForm: () => null }));

import LoginPage from "./page";

describe("ログインページの復帰先と戻り案内", () => {
  it.each([
    [
      "/news/../watchlist?_rsc=a&page=2#saved",
      {
        returnTo: "/watchlist?page=2#saved",
        requiresLoginReason: true,
        backHref: "/",
        backLabel: "ニュースへ戻る",
      },
    ],
    [
      "/news/1",
      {
        returnTo: "/news/1",
        requiresLoginReason: true,
        backHref: "/news/1",
        backLabel: "元のページへ戻る",
      },
    ],
    [
      "/research",
      {
        returnTo: "/research",
        requiresLoginReason: true,
        backHref: "/",
        backLabel: "ニュースへ戻る",
      },
    ],
    [
      "/watchlist?page=2",
      {
        returnTo: "/watchlist?page=2",
        requiresLoginReason: true,
        backHref: "/",
        backLabel: "ニュースへ戻る",
      },
    ],
    [
      ["/watchlist", "/research"],
      {
        returnTo: "/",
        requiresLoginReason: false,
        backHref: "/",
        backLabel: "ニュースへ戻る",
      },
    ],
    [
      "https://evil.test",
      {
        returnTo: "/",
        requiresLoginReason: false,
        backHref: "/",
        backLabel: "ニュースへ戻る",
      },
    ],
    [
      "/%61uth/login",
      {
        returnTo: "/",
        requiresLoginReason: false,
        backHref: "/",
        backLabel: "ニュースへ戻る",
      },
    ],
    [
      undefined,
      {
        returnTo: "/",
        requiresLoginReason: false,
        backHref: "/",
        backLabel: "ニュースへ戻る",
      },
    ],
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
    expect(page.props.children.props).toMatchObject(expected);
  });
});
