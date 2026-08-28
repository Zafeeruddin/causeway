import type { NextConfig } from "next";

const API = process.env.API_ORIGIN ?? "http://localhost:8000";
const MEDIAMTX = process.env.MEDIAMTX_ORIGIN ?? "http://localhost:8889";

const config: NextConfig = {
  reactStrictMode: true,
  // Proxy the API through this origin so the session cookie stays first-party.
  // Pointing the browser straight at :8000 would make it cross-site, and a
  // SameSite=Lax cookie would silently stop being sent.
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API}/api/:path*` },
      // WebRTC signalling, same-origin for the same reason. The server cannot
      // know what address the browser used to reach this app -- every request
      // arrives through the rewrite above -- so a preview URL derived from the
      // Host header works only on the machine running the stack. Only the offer
      // and answer go through here; the video itself flows straight from
      // MediaMTX to the browser over UDP.
      { source: "/rtc/:path*", destination: `${MEDIAMTX}/:path*` },
    ];
  },
};

export default config;
