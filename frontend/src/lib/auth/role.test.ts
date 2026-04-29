import { describe, expect, it } from "vitest";
import { ALLOWED_ROLES, narrowRole } from "./role";

describe("narrowRole", () => {
  it("passes through 'user'", () => {
    expect(narrowRole("user")).toBe("user");
  });

  it("passes through 'admin'", () => {
    expect(narrowRole("admin")).toBe("admin");
  });

  it("downgrades unknown role to 'user' (fail-safe)", () => {
    expect(narrowRole("superadmin")).toBe("user");
    expect(narrowRole("guest")).toBe("user");
    expect(narrowRole("")).toBe("user");
  });

  it("treats casing as unknown (allowlist is case-sensitive)", () => {
    // Better Auth が "Admin" を返してきても admin 権限は付与されない
    expect(narrowRole("Admin")).toBe("user");
    expect(narrowRole("ADMIN")).toBe("user");
    expect(narrowRole("User")).toBe("user");
  });
});

describe("ALLOWED_ROLES", () => {
  it("matches backend UserRole exactly (regression guard)", () => {
    // backend/app/dependencies.py の UserRole と同期されていることを構造的に保証する。
    // 順序を含めた完全一致 — 増減は backend と同時更新が必要。
    expect(ALLOWED_ROLES).toEqual(["user", "admin"]);
  });
});
