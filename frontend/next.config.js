/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  experimental: {
    optimizePackageImports: ["radix-ui", "lucide-react"],
  },
};

module.exports = nextConfig;
