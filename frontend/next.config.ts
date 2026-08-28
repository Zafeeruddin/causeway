import type { NextConfig } from "next";

const API = process.env.API_ORIGIN ?? "http://localhost:8000";

const config: NextConfig = {
  reactStrictMode: true,
  // Proxy the API through this origin so the session cookie stays first-party.
  // Pointing the browser straight at :8000 would make it cross-site, and a
  // SameSite=Lax cookie would silently stop being sent.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API}/api/:path*` }];
  },
};

export default config;
