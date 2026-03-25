import { betterAuth } from "better-auth";
import type { PoolClient } from "pg";
import { Pool } from "pg";

const pool = new Pool({
  connectionString: process.env.AUTH_DATABASE_URL,
});

// Direct all Better Auth queries to the 'auth' schema
pool.on("connect", (client: PoolClient) => {
  client.query("SET search_path TO auth, public");
});

export const auth = betterAuth({
  database: pool,
  basePath: "/api/auth",
  emailAndPassword: {
    enabled: true,
    minPasswordLength: 8,
  },
  user: {
    additionalFields: {
      role: {
        type: "string",
        defaultValue: "user",
        input: false,
      },
    },
  },
  session: {
    cookieCache: {
      enabled: true,
      maxAge: 5 * 60, // 5 minutes
    },
  },
  trustedOrigins: [process.env.BETTER_AUTH_URL ?? "http://localhost:3000"],
  advanced: {
    database: {
      generateId: "uuid",
    },
  },
});
