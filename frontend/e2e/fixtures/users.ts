// Defaults match local seed users; CI can override them via env.
export const USER = {
  email: process.env.E2E_USER_EMAIL ?? "e2e@example.com",
  password: process.env.E2E_USER_PASSWORD ?? "Password123!",
} as const;

export const ADMIN_USER = {
  email: process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@example.com",
  password: process.env.E2E_ADMIN_PASSWORD ?? "Password123!",
} as const;
