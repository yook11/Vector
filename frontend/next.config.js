const e2eNextDistDir = process.env.E2E_NEXT_DIST_DIR;
if (
  e2eNextDistDir !== undefined &&
  !/^\.e2e-next\/[a-z0-9-]+-\d+-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
    e2eNextDistDir,
  )
) {
  throw new Error("E2E_NEXT_DIST_DIR must be an .e2e-next scenario directory");
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  ...(e2eNextDistDir === undefined ? {} : { distDir: e2eNextDistDir }),
  output: "standalone",
  cacheComponents: true,
  experimental: {
    optimizePackageImports: ["radix-ui", "lucide-react"],
    sri: {
      algorithm: "sha256",
    },
  },
  async headers() {
    return [
      {
        // manifest は icon hash の参照更新を 1 時間以内に反映し、画像本体は immutable cache に任せる。
        source: "/manifest.webmanifest",
        headers: [
          {
            key: "Cache-Control",
            value: "public, max-age=3600, must-revalidate",
          },
        ],
      },
      {
        // 認証応答は session 失効や権限変更を即時反映するため、ブラウザにも共有 cache にも保存させない。
        source: "/api/auth/:path*",
        headers: [
          {
            key: "Cache-Control",
            value: "private, no-store",
          },
        ],
      },
    ];
  },
};

module.exports = nextConfig;
