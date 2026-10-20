/** @type {import('next').NextConfig} */
const backend = process.env.BACKEND_INTERNAL_URL || "http://localhost:8000";

const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  // FastAPI collection routes end with "/" — keep the slash instead of redirecting.
  skipTrailingSlashRedirect: true,
  // Dev convenience: proxy /api/* to FastAPI so cookies are same-origin.
  // In Docker/production Caddy routes /api/* to the backend before it reaches Next.js.
  async rewrites() {
    return [
      { source: "/api/:path*/", destination: `${backend}/api/:path*/` },
      { source: "/api/:path*", destination: `${backend}/api/:path*` },
    ];
  },
};

export default nextConfig;
