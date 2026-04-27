import { inferAdditionalFields } from "better-auth/client/plugins";
import { createAuthClient } from "better-auth/react";
import type { auth } from "@/lib/auth/auth";

// Re-entry guard for the 401 redirect path. The flag stays "true" only until
// window.location.href triggers a full reload, which resets module state.
let signingOut = false;

export const authClient = createAuthClient({
  basePath: "/api/auth",
  plugins: [inferAdditionalFields<typeof auth>()],
  fetchOptions: {
    onError(ctx) {
      if (typeof window === "undefined") return;
      if (ctx.response?.status !== 401) return;
      if (signingOut) return;
      // Don't loop on the auth pages themselves.
      if (window.location.pathname.startsWith("/auth/")) return;
      signingOut = true;
      authClient.signOut().finally(() => {
        window.location.href = "/auth/login";
      });
    },
  },
});

export const { signIn, signUp, signOut, useSession } = authClient;
