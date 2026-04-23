module.exports = {
  apps: [
    {
      name: "uni-api",
      script: "./artifacts/api-server/dist/index.mjs",
      interpreter: "node",
      cwd: __dirname,
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: "2048M",
      env: {
        NODE_ENV: "production",
        PORT: "8080",
        GEMINI_API_KEY: process.env.GEMINI_API_KEY || "",
        DATABASE_URL: process.env.DATABASE_URL || "",
        SESSION_SECRET: process.env.SESSION_SECRET || "",
        // Bump libuv threadpool from default 4 → 16 so the 4-vCPU box can
        // actually fan out parallel DNS lookups, file I/O and TLS handshakes
        // (otherwise even with HTTP CONCURRENCY=32 we serialize on 4 libuv
        // threads, which is why CPU was capping at ~53% during scrapes).
        UV_THREADPOOL_SIZE: "16",
        // 4 GB heap headroom so big batches with browser fetches don't OOM
        // before max_memory_restart kicks in.
        NODE_OPTIONS: "--max-old-space-size=4096",
      },
    },
  ],
};
