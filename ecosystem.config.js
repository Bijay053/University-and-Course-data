module.exports = {
  apps: [
    {
      name: "api-py",
      script: "/root/University-and-Course-data/backend-py/start.sh",
      cwd: "/root/University-and-Course-data/backend-py",
      interpreter: "bash",
      autorestart: true,
      watch: false,
      env: {
        PYTHONPATH: "/root/University-and-Course-data/backend-py"
      }
    }
  ]
};
