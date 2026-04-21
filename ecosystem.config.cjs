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
      env_file: "./.env",
      env: {
        NODE_ENV: "production",
        PORT: "8080",
      },
    },
  ],
};
