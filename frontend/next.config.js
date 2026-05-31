/** @type {import('next').NextConfig} */
const nextConfig = {
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
