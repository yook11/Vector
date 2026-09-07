export const REQUEST_PATH_HEADER = "x-vector-request-path";

export function isPublicPage(pathname: string): boolean {
  return (
    ["/", "/trends", "/briefing"].includes(pathname) ||
    /^\/(news|briefing)\/[^/]+$/.test(pathname)
  );
}

export function isPersonalPage(pathname: string): boolean {
  return (
    pathname === "/watchlist" ||
    pathname === "/research" ||
    pathname.startsWith("/research/")
  );
}

const PUBLIC_ASSET_PATHS = new Set([
  "/favicon.ico",
  "/icon.svg",
  "/apple-icon.png",
  "/opengraph-image.png",
  "/twitter-image.png",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
]);

export function isPublicAsset(pathname: string): boolean {
  return PUBLIC_ASSET_PATHS.has(pathname);
}
