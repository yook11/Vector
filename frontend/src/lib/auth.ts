import type { NextAuthOptions } from "next-auth";
import CredentialsProvider from "next-auth/providers/credentials";

/**
 * Backend API base URL for server-side auth calls.
 * Uses INTERNAL_API_URL (Docker internal) or NEXT_PUBLIC_API_URL.
 */
function getAuthApiUrl(): string {
  return (
    process.env.INTERNAL_API_URL ??
    process.env.NEXT_PUBLIC_API_URL ??
    "http://localhost:8000/api/v1"
  );
}

export const authOptions: NextAuthOptions = {
  providers: [
    CredentialsProvider({
      name: "credentials",
      credentials: {
        email: { label: "Email", type: "email" },
        password: { label: "Password", type: "password" },
      },
      async authorize(credentials) {
        if (!credentials?.email || !credentials?.password) return null;

        try {
          const res = await fetch(`${getAuthApiUrl()}/auth/login`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              email: credentials.email,
              password: credentials.password,
            }),
          });

          if (!res.ok) return null;

          const data = await res.json();
          // Return user object with tokens for JWT callback
          return {
            id: "temp", // Will be decoded from access token
            accessToken: data.accessToken,
            refreshToken: data.refreshToken,
          };
        } catch {
          return null;
        }
      },
    }),
  ],

  session: {
    strategy: "jwt",
    maxAge: 30 * 24 * 60 * 60, // 30 days
  },

  pages: {
    signIn: "/auth/login",
  },

  callbacks: {
    async jwt({ token, user }) {
      // Initial sign-in: transfer tokens from authorize() result
      if (user) {
        token.accessToken = user.accessToken;
        token.refreshToken = user.refreshToken;

        // Decode user info from access token payload
        try {
          const payload = JSON.parse(
            Buffer.from(
              (user.accessToken as string).split(".")[1],
              "base64",
            ).toString(),
          );
          token.userId = payload.sub;
          token.email = payload.email;
          token.accessTokenExpires = payload.exp * 1000;
        } catch {
          // If decode fails, keep going
        }
      }

      // Return token if not expired
      if (Date.now() < (token.accessTokenExpires as number ?? 0)) {
        return token;
      }

      // Access token expired — try to refresh
      try {
        const res = await fetch(`${getAuthApiUrl()}/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            refreshToken: token.refreshToken,
          }),
        });

        if (!res.ok) throw new Error("Refresh failed");

        const data = await res.json();
        const payload = JSON.parse(
          Buffer.from(data.accessToken.split(".")[1], "base64").toString(),
        );

        return {
          ...token,
          accessToken: data.accessToken,
          refreshToken: data.refreshToken,
          accessTokenExpires: payload.exp * 1000,
        };
      } catch {
        // Refresh failed — clear tokens and force re-login
        return {
          ...token,
          accessToken: undefined,
          refreshToken: undefined,
          error: "RefreshTokenError",
        };
      }
    },

    async session({ session, token }) {
      session.accessToken = (token.accessToken as string) ?? "";
      session.error = token.error as string | undefined;
      if (token.userId) {
        session.user = {
          ...session.user,
          id: token.userId as string,
          email: token.email as string,
        };
      }
      return session;
    },
  },
};
