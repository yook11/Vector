import { describe, expect, it } from "vitest";
import * as authSchemas from "./auth";

const { LoginSchema } = authSchemas;

type ProvisionUserInput = {
  name: string;
  email: string;
  password: string;
};

type ProvisionUserSchema = {
  safeParse(
    input: unknown,
  ): { success: true; data: ProvisionUserInput } | { success: false };
};

const validProvisionUserInput: ProvisionUserInput = {
  name: "山田 太郎_2-テスト",
  email: "user@example.com",
  password: "password",
};

function provisionUserSchema(): ProvisionUserSchema {
  const schema = Reflect.get(authSchemas, "ProvisionUserSchema");
  expect(schema).toMatchObject({ safeParse: expect.any(Function) });
  return schema as ProvisionUserSchema;
}

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

describe("ProvisionUserSchema", () => {
  it("accepts the three fields, trims name, lowercases email, and preserves password", () => {
    const result = provisionUserSchema().safeParse({
      name: "  山田 太郎_2-テスト  ",
      email: "  USER@EXAMPLE.COM  ",
      password: " 123456 ",
    });

    expect(result).toEqual({
      success: true,
      data: {
        name: "山田 太郎_2-テスト",
        email: "user@example.com",
        password: " 123456 ",
      },
    });
  });

  it("accepts and preserves a Japanese name containing a full-width space", () => {
    const result = provisionUserSchema().safeParse({
      ...validProvisionUserInput,
      name: "山田　太郎",
    });

    expect(result).toEqual({
      success: true,
      data: { ...validProvisionUserInput, name: "山田　太郎" },
    });
  });

  it.each([1, 100])("accepts a name with %i characters", (length) => {
    const result = provisionUserSchema().safeParse({
      ...validProvisionUserInput,
      name: "あ".repeat(length),
    });

    expect(result.success).toBe(true);
  });

  it.each([
    ["empty after trimming", "   "],
    ["101 characters after trimming", "あ".repeat(101)],
    ["disallowed punctuation", "山田!太郎"],
    ["a line break", "山田\n太郎"],
  ])("rejects a name with %s", (_reason, name) => {
    const result = provisionUserSchema().safeParse({
      ...validProvisionUserInput,
      name,
    });

    expect(result.success).toBe(false);
  });

  it("rejects an email that is invalid after trimming", () => {
    const result = provisionUserSchema().safeParse({
      ...validProvisionUserInput,
      email: "  not-an-email  ",
    });

    expect(result.success).toBe(false);
  });

  it.each([8, 128])("accepts a password with %i characters", (length) => {
    const result = provisionUserSchema().safeParse({
      ...validProvisionUserInput,
      password: "p".repeat(length),
    });

    expect(result.success).toBe(true);
  });

  it.each([7, 129])("rejects a password with %i characters", (length) => {
    const result = provisionUserSchema().safeParse({
      ...validProvisionUserInput,
      password: "p".repeat(length),
    });

    expect(result.success).toBe(false);
  });

  it.each([
    ["role", "admin"],
    ["data", { role: "admin" }],
    ["id", "019f7eea-1030-7f00-8e89-d274f4f1f0b8"],
    ["providerId", "credential"],
  ])("rejects the unknown %s field", (field, value) => {
    const result = provisionUserSchema().safeParse({
      ...validProvisionUserInput,
      [field]: value,
    });

    expect(result.success).toBe(false);
  });
});
