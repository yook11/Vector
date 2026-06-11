import { test as setup } from "@playwright/test";
import { ADMIN_USER, USER } from "./fixtures/users";

const userFile = "e2e/.auth/user.json";
const adminFile = "e2e/.auth/admin.json";

// setup project は programmatic login、login.spec.ts は UI login regression を担当する。
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
