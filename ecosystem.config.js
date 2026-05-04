module.exports = {
  apps: [
    {
      name: "api-py",
      script: "/root/University-and-Course-data/backend-py/venv/bin/uvicorn",
      args: "app.main:app --host 0.0.0.0 --port 8080 --workers 1",
      cwd: "/root/University-and-Course-data/backend-py",
      interpreter: "none",
      autorestart: true,
      watch: false,
      env: {
        PYTHONPATH: "/root/University-and-Course-data/backend-py"
      }
    }
  ]
};
