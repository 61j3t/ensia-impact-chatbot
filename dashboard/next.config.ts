import type { NextConfig } from "next";

const config: NextConfig = {
  // postgres.js binds to native sockets; keep it out of the Edge bundle.
  serverExternalPackages: ["postgres"],
  async redirects() {
    return [
      // /overview, /users, /conversations were merged into / (the new
      // home). Permanent 308 redirects keep the query string intact so
      // any /users?user=123 bookmarks still land on the right transcript.
      { source: "/overview", destination: "/", permanent: true },
      { source: "/users", destination: "/", permanent: true },
      { source: "/conversations", destination: "/", permanent: true },
    ];
  },
};

export default config;
