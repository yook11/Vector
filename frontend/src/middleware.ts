export { default } from "next-auth/middleware";

export const config = {
  // Protect all routes except auth pages, API routes, and static assets
  matcher: [
    "/((?!auth|api|_next/static|_next/image|favicon.ico).*)",
  ],
};
