#!/bin/bash
set -e

echo "=== 1. System update ==="
apt-get update -y && apt-get upgrade -y
apt-get install -y curl git nginx

echo "=== 2. Node.js 24 ==="
curl -fsSL https://deb.nodesource.com/setup_24.x | bash -
apt-get install -y nodejs

echo "=== 3. pnpm ==="
npm install -g pnpm@10

echo "=== 4. PostgreSQL ==="
apt-get install -y postgresql postgresql-contrib
systemctl start postgresql
systemctl enable postgresql

# Idempotent: create user and database only if they don't exist
sudo -u postgres psql <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'uniportal') THEN
    CREATE USER uniportal WITH PASSWORD 'yourpassword123';
  END IF;
END
$$;
SQL
sudo -u postgres createdb -O uniportal university_portal 2>/dev/null || echo "DB already exists — skipping"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE university_portal TO uniportal;"

echo "=== 5. Playwright/Chromium dependencies ==="
# libasound2 was renamed to libasound2t64 in Ubuntu 24 — install whichever exists
apt-get install -y \
  libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
  libxrandr2 libgbm1 libpango-1.0-0 libpangocairo-1.0-0 \
  libcairo2 libatspi2.0-0 libgtk-3-0 libgdk-pixbuf2.0-0 fonts-liberation
apt-get install -y libasound2 2>/dev/null || apt-get install -y libasound2t64 2>/dev/null || true

echo "=== 6. Clone repo ==="
if [ -d /opt/app/.git ]; then
  echo "Repo already exists — pulling latest changes"
  # Remove any locally modified tracked files that would block the pull
  git -C /opt/app checkout -- deploy.sh 2>/dev/null || true
  # Remove untracked deploy.sh if git checkout didn't handle it
  [ -f /opt/app/deploy.sh ] && git -C /opt/app ls-files --error-unmatch deploy.sh 2>/dev/null || rm -f /opt/app/deploy.sh
  git -C /opt/app pull
else
  git clone https://github.com/Bijay053/University-and-Course-data.git /opt/app
fi
cd /opt/app

echo "=== 7. Environment variables ==="
# Only write .env if it doesn't already contain real values
if grep -q "yourpassword123\|your_gemini_api_key_here\|change_this_to_a_random" /opt/app/.env 2>/dev/null || [ ! -f /opt/app/.env ]; then
  echo "Writing .env with placeholder values — EDIT THIS FILE before first run!"
  cat > /opt/app/.env <<'ENV'
DATABASE_URL=postgresql://uniportal:yourpassword123@localhost:5432/university_portal
GEMINI_API_KEY=your_gemini_api_key_here
SESSION_SECRET=change_this_to_a_random_secret_string_64chars
PORT=8080
NODE_ENV=production
ENV
else
  echo ".env already configured — keeping existing values"
fi

echo "=== 8. Install dependencies ==="
cd /opt/app
pnpm install

echo "=== 9. Install Playwright Chromium ==="
cd /opt/app
npx playwright install chromium

echo "=== 10. Run DB migration ==="
cd /opt/app
# Load .env safely (handles spaces, quotes, comments)
set -a; source .env; set +a
pnpm --filter @workspace/db run push

echo "=== 11. Build API server ==="
cd /opt/app
set -a; source .env; set +a
pnpm --filter @workspace/api-server run build

echo "=== 12. Build frontend ==="
# PORT and BASE_PATH are REQUIRED by the Vite config — without them the build fails.
# PORT just needs any number (it sets the dev-server port, irrelevant in build mode).
# BASE_PATH=/ means the app is served from the root of the domain.
cd /opt/app
PORT=3000 BASE_PATH=/ NODE_ENV=production pnpm --filter @workspace/university-portal run build

echo "=== 13. PM2 process manager ==="
npm install -g pm2

# Source env vars from .env into the PM2 ecosystem config
source /opt/app/.env

cat > /opt/app/ecosystem.config.cjs <<ECOSYSTEM
module.exports = {
  apps: [{
    name: 'api-server',
    script: '/opt/app/artifacts/api-server/dist/index.mjs',
    node_args: '--enable-source-maps',
    cwd: '/opt/app/artifacts/api-server',
    env: {
      NODE_ENV: 'production',
      PORT: '8080',
      DATABASE_URL: '${DATABASE_URL}',
      GEMINI_API_KEY: '${GEMINI_API_KEY}',
      SESSION_SECRET: '${SESSION_SECRET}'
    }
  }]
};
ECOSYSTEM

pm2 start /opt/app/ecosystem.config.cjs
pm2 save
pm2 startup

echo "=== 14. Nginx config ==="
# IMPORTANT: The Vite build outputs to dist/public (not dist directly)
cat > /etc/nginx/sites-available/uniportal <<'NGINX'
server {
    listen 80;
    server_name _;

    # Frontend static files — Vite builds to dist/public
    root /opt/app/artifacts/university-portal/dist/public;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    # API proxy — forwards /api/* to the Node.js API server
    location /api/ {
        proxy_pass http://localhost:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_cache_bypass $http_upgrade;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        client_max_body_size 50M;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/uniportal /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx
systemctl enable nginx

echo ""
echo "==================================================================="
echo " DONE — App is running at http://YOUR_DROPLET_IP"
echo "==================================================================="
echo ""
echo " Before running this script, replace these placeholders in .env:"
echo "   GEMINI_API_KEY       → your real Gemini API key"
echo "   SESSION_SECRET       → a random 64-char string"
echo "   yourpassword123      → a strong Postgres password"
echo ""
echo " Useful commands:"
echo "   pm2 logs api-server       — tail API logs"
echo "   pm2 restart api-server    — restart after code changes"
echo "   pm2 status                — show process status"
echo "   nginx -t                  — test Nginx config"
echo "   systemctl status nginx    — check Nginx status"
echo "==================================================================="
