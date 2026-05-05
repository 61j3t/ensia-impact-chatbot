import type { NextConfig } from "next";

const config: NextConfig = {
  // postgres.js binds to native sockets; keep it out of the Edge bundle.
  serverExternalPackages: ["postgres"],
};

export default config;
