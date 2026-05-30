import { describe, expect, it } from "vitest";
import { LoginSchema, RegisterSchema } from "./auth";

describe("LoginSchema", () => {
  it("accepts valid email + password", () => {
    const result = LoginSchema.safeParse({
      email: "user@example.com",
      password: "anything",
    });
    expect(result.success).toBe(true);
  });

  it("trims surrounding whitespace from email", () => {
    const result = LoginSchema.safeParse({
      email: "  user@example.com  ",
      password: "x",
    });
    expect(result.success).toBe(true);
    if (result.success) expect(result.data.email).toBe("user@example.com");
  });

  it("rejects an invalid email", () => {
    const result = LoginSchema.safeParse({
      email: "not-an-email",
      password: "x",
    });
    expect(result.success).toBe(false);
  });

  it("rejects an empty password", () => {
    const result = LoginSchema.safeParse({
      email: "user@example.com",
      password: "",
    });
    expect(result.success).toBe(false);
  });
});

describe("RegisterSchema", () => {
  const VALID_INPUT = {
    email: "user@example.com",
    password: "longenoughpw",
    displayName: "Alice",
  } as const;

  it("accepts a fully valid payload", () => {
    expect(RegisterSchema.safeParse(VALID_INPUT).success).toBe(true);
  });

  describe("password", () => {
    it("rejects a 7-character password", () => {
      const result = RegisterSchema.safeParse({
        ...VALID_INPUT,
        password: "1234567",
      });
      expect(result.success).toBe(false);
    });

    it("rejects a 129-character password", () => {
      const result = RegisterSchema.safeParse({
        ...VALID_INPUT,
        password: "a".repeat(129),
      });
      expect(result.success).toBe(false);
    });
  });

  describe("displayName", () => {
    it("accepts unicode characters (e.g. Japanese)", () => {
      const result = RegisterSchema.safeParse({
        ...VALID_INPUT,
        displayName: "テックニュース",
      });
      expect(result.success).toBe(true);
    });

    it("rejects characters outside the allowlist", () => {
      // Unicode regex で allowlist 外の文字を拒否する。
      expect(
        RegisterSchema.safeParse({
          ...VALID_INPUT,
          displayName: "<script>",
        }).success,
      ).toBe(false);
      expect(
        RegisterSchema.safeParse({
          ...VALID_INPUT,
          displayName: "Tech!Crunch",
        }).success,
      ).toBe(false);
    });

    it("normalizes empty string to undefined", () => {
      const result = RegisterSchema.safeParse({
        ...VALID_INPUT,
        displayName: "",
      });
      expect(result.success).toBe(true);
      if (result.success) expect(result.data.displayName).toBeUndefined();
    });

    it("trims surrounding whitespace", () => {
      const result = RegisterSchema.safeParse({
        ...VALID_INPUT,
        displayName: "  Alice  ",
      });
      expect(result.success).toBe(true);
      if (result.success) expect(result.data.displayName).toBe("Alice");
    });

    it("rejects names exceeding 100 characters", () => {
      const result = RegisterSchema.safeParse({
        ...VALID_INPUT,
        displayName: "a".repeat(101),
      });
      expect(result.success).toBe(false);
    });

    it("treats omitted displayName as undefined", () => {
      const { displayName: _omit, ...rest } = VALID_INPUT;
      const result = RegisterSchema.safeParse(rest);
      expect(result.success).toBe(true);
      if (result.success) expect(result.data.displayName).toBeUndefined();
    });
  });
});
