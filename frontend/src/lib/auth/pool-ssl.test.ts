import { describe, expect, it } from "vitest";
import { poolConfigFromUrl } from "./pool-ssl";

describe("poolConfigFromUrl", () => {
  it("sslmode=require は ssl 検証を有効化し sslmode を connectionString から除く", () => {
    const { connectionString, ssl } = poolConfigFromUrl(
      "postgresql://u:p@ep-x.aws.neon.tech/neondb?sslmode=require",
    );

    expect(ssl).toEqual({ rejectUnauthorized: true });
    expect(connectionString).not.toContain("sslmode");
  });

  it("Neon の channel_binding も pg に渡さない (未対応 param 排除)", () => {
    const { connectionString } = poolConfigFromUrl(
      "postgresql://u:p@ep-x.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
    );

    expect(connectionString).not.toContain("channel_binding");
    expect(connectionString).not.toContain("sslmode");
  });

  it("sslmode なし (dev / docker) は SSL を無効化する", () => {
    const { ssl } = poolConfigFromUrl("postgresql://u:p@db:5432/vector");

    expect(ssl).toBe(false);
  });

  it("sslmode=disable は明示的に SSL を無効化する", () => {
    const { ssl } = poolConfigFromUrl(
      "postgresql://u:p@db:5432/vector?sslmode=disable",
    );

    expect(ssl).toBe(false);
  });

  it("search_path など他の query param は保持する", () => {
    const { connectionString } = poolConfigFromUrl(
      "postgresql://u:p@db:5432/vector?search_path=auth",
    );

    expect(connectionString).toContain("search_path=auth");
  });
});
