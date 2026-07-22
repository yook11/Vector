import "server-only";

import { v7 as uuidv7 } from "uuid";
import { authPool } from "@/lib/auth/auth-db";
import { hashPassword } from "@/lib/auth/password";

type CreateUserWithCredentialInput = {
  name: string;
  email: string;
  password: string;
};

type CreateUserWithCredentialErrorCode = "duplicate-email" | "internal";

export class CreateUserWithCredentialError extends Error {
  readonly code: CreateUserWithCredentialErrorCode;

  constructor(code: CreateUserWithCredentialErrorCode) {
    super(
      code === "duplicate-email"
        ? "A user with this email already exists."
        : "Unable to provision user.",
    );
    this.name = "CreateUserWithCredentialError";
    this.code = code;
  }
}

function classifyCreateUserWithCredentialError(
  error: unknown,
): CreateUserWithCredentialErrorCode {
  if (typeof error !== "object" || error === null) {
    return "internal";
  }

  const databaseError = error as { code?: unknown; constraint?: unknown };
  return databaseError.code === "23505" &&
    databaseError.constraint === "user_email_key"
    ? "duplicate-email"
    : "internal";
}

export async function createUserWithCredential({
  name,
  email,
  password,
}: CreateUserWithCredentialInput): Promise<void> {
  try {
    const passwordHash = await hashPassword(password);
    const userId = uuidv7();
    const accountId = uuidv7();
    const now = new Date();
    const client = await authPool.connect();
    let transactionStarted = false;
    let destroyClient = false;

    try {
      await client.query("BEGIN");
      transactionStarted = true;
      await client.query(
        `INSERT INTO auth."user"
          (id, name, email, "emailVerified", "createdAt", "updatedAt", role)
         VALUES ($1::uuid, $2, $3, false, $4, $4, 'user')`,
        [userId, name, email, now],
      );
      await client.query(
        `INSERT INTO auth.account
          (id, "accountId", "providerId", "userId", password, "createdAt", "updatedAt")
         VALUES ($1::uuid, $2, 'credential', $3::uuid, $4, $5, $5)`,
        [accountId, userId, userId, passwordHash, now],
      );
      await client.query("COMMIT");
      transactionStarted = false;
    } catch (error) {
      if (transactionStarted) {
        try {
          await client.query("ROLLBACK");
        } catch {
          destroyClient = true;
        }
      }
      throw error;
    } finally {
      if (destroyClient) {
        client.release(true);
      } else {
        client.release();
      }
    }
  } catch (error) {
    throw new CreateUserWithCredentialError(
      classifyCreateUserWithCredentialError(error),
    );
  }
}
