import { describe, expect, it } from "vitest";
import { LoginCallbackSchema, parseLoginCallback } from "./login-callback";

const accepted = [
  ["/", "/"],
  ["/news/1", "/news/1"],
  ["/news/../watchlist", "/watchlist"],
  ["/a/%2e%2e/research", "/research"],
  ["/news/hello world", "/news/hello%20world"],
  ["/watchlist?page=2&page=3#saved", "/watchlist?page=2&page=3#saved"],
  [
    "/news?q=日本語#見出し",
    "/news?q=%E6%97%A5%E6%9C%AC%E8%AA%9E#%E8%A6%8B%E5%87%BA%E3%81%97",
  ],
  ["/watchlist?_rsc=a&page=2&_rsc=b#saved", "/watchlist?page=2#saved"],
  ["/?%5Frsc=a&q=one&q=two", "/?q=one&q=two"],
  ["/?_rsc=a", "/"],
  ["/?q=a%26b%3Dc%23d", "/?q=a%26b%3Dc%23d"],
  [
    "/?url=https%3A%2F%2Fexample.com%2Fa",
    "/?url=https%3A%2F%2Fexample.com%2Fa",
  ],
  ["/?q=%252F#%252F", "/?q=%252F#%252F"],
  ["/author", "/author"],
] as const;

const rejected: unknown[] = [
  undefined,
  null,
  1,
  true,
  {},
  [],
  ["/"],
  "",
  " /",
  "/ ",
  "relative",
  "#heading",
  "https://vector.invalid/news/1",
  "https://evil.test",
  "//evil.test",
  "javascript:alert(1)",
  "/\\evil.test",
  "/news\n",
  "/news\t",
  "/news\u0000",
  "/news\u007f",
  "/%5Cevil",
  "/%2Fevil",
  "/news%2f1",
  "/news%3fadmin",
  "/news%23heading",
  "/%252fexample.com",
  "/%2561uth/login",
  "/%25252fexample.com",
  "/%",
  "/%GG",
  "/%C0%AF",
  "/?q=%E3%81",
  "/#%FF",
  "/?q=%5C",
  "/#%0A",
  "/?q=%7f",
  "/bad%0a/../watchlist",
  "/auth",
  "/auth/",
  "/auth/login",
  "/auth/register?callbackUrl=/",
  "/news/../auth/login",
  "/%61uth/login",
  "/a/%2e%2e/auth/login",
  "/.//evil.test",
  "/x/..//evil.test",
];

describe("LoginCallbackSchema", () => {
  it.each(accepted)("%s を %s へ正規化する", (input, expected) => {
    const result = LoginCallbackSchema.safeParse(input);
    expect(result.success).toBe(true);
    if (!result.success) return;
    expect(result.data).toBe(expected);
    expect(LoginCallbackSchema.parse(result.data)).toBe(result.data);
    expect(new URL(result.data, "https://vector.invalid").origin).toBe(
      "https://vector.invalid",
    );
    expect(parseLoginCallback(input)).toBe(result.data);
  });
  it.each(
    rejected.map((value) => [value]),
  )("危険・不正な入力 %j を拒否する", (input) => {
    expect(() => LoginCallbackSchema.safeParse(input)).not.toThrow();
    expect(LoginCallbackSchema.safeParse(input).success).toBe(false);
    expect(parseLoginCallback(input)).toBeNull();
  });
  it("全ASCII制御文字を生値とエンコードの両方で拒否する", () => {
    for (const code of [
      ...Array.from({ length: 32 }, (_, index) => index),
      127,
    ]) {
      for (const value of [
        String.fromCharCode(code),
        `%${code.toString(16).padStart(2, "0")}`,
      ]) {
        for (const input of [`/news${value}`, `/?q=${value}`, `/#${value}`]) {
          expect(
            LoginCallbackSchema.safeParse(input).success,
            JSON.stringify(input),
          ).toBe(false);
        }
      }
    }
  });
  it("クエリの区切り・重複・順序を維持する", () => {
    const result = LoginCallbackSchema.parse(
      "/?q=a%26b%3Dc%23d&_rsc=1&q=日本語&url=https%3A%2F%2Fexample.com",
    );
    expect([...new URL(result, "https://vector.invalid").searchParams]).toEqual(
      [
        ["q", "a&b=c#d"],
        ["q", "日本語"],
        ["url", "https://example.com"],
      ],
    );
  });
});
