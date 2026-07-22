import { describe, expect, it } from "vitest";
import { LoginSchema } from "./auth";

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
