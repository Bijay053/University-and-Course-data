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
      max_memory_restart: "512M",
      env: {
        NODE_ENV: "production",
        PORT: "8080",
        GEMINI_API_KEY: process.env.GEMINI_API_KEY || "",
        DATABASE_URL: process.env.DATABASE_URL || "",
        SESSION_SECRET: process.env.SESSION_SECRET || "",
      },
    },
  ],
};
