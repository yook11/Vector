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
};

module.exports = nextConfig;
