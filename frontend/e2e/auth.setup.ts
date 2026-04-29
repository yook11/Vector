import { test as setup } from "@playwright/test";
import { ADMIN_USER, USER } from "./fixtures/users";

const userFile = "e2e/.auth/user.json";
const adminFile = "e2e/.auth/admin.json";

// programmatic login: Better Auth `/api/auth/sign-in/email` に直接 POST して
// session cookie を取得し、storageState として永続化する。UI 経由の login spec
// (login.spec.ts) はあえて storageState なしで残し、login flow 自体の regression
// 検知を維持する。
setup("authenticate user", async ({ request }) => {
  const res = await request.post("/api/auth/sign-in/email", {
    data: { email: USER.email, password: USER.password },
  });
  if (!res.ok()) {
    const body = await res.text();
    throw new Error(
      `User sign-in failed: ${res.status()} ${res.statusText()}\n${body}`,
    );
  }
  await request.storageState({ path: userFile });
});

setup("authenticate admin", async ({ request }) => {
  const res = await request.post("/api/auth/sign-in/email", {
    data: { email: ADMIN_USER.email, password: ADMIN_USER.password },
  });
  if (!res.ok()) {
    const body = await res.text();
    throw new Error(
      `Admin sign-in failed: ${res.status()} ${res.statusText()}\n${body}`,
    );
  }
  await request.storageState({ path: adminFile });
});
