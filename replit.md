# University Course, Fee, Intake & Requirement Management System

## Overview

This project provides a centralized administrative portal for universities to manage their course-related data. It enables comprehensive management of courses, fees, intakes, scholarships, and admission requirements. Key capabilities include AI-powered web scraping for data acquisition, bulk upload/download functionalities, and change detection mechanisms. The system aims to streamline data management for educational institutions, offering a robust solution for maintaining up-to-date and accurate course information.

## User Preferences

- Provide commands in the format `cd /root/University-and-Course-data && <command>`.
- Always provide commands in the specified format, especially for production deployment and verification.
- When schema changes are needed, explicitly provide the `pnpm --filter @workspace/db push --force` command before builds.
- The Node.js API server has been deleted. Python FastAPI is now the sole API server in both dev and production.
- Provide verification commands to confirm commit deployment, new bundle serving, and correct PM2 environment variables.

## System Architecture

The system is built as a monorepo utilizing `pnpm workspaces`.

### Technology Stack

- **Frontend**: React with Vite, styled using Tailwind CSS and `shadcn/ui`. Data fetching is managed by TanStack React Query, and routing by `wouter`.
- **Backend**: FastAPI (Python / Uvicorn) serving on port 8080 — both dev and production. Node.js API server has been deleted.
- **Database**: PostgreSQL with Drizzle ORM for type-safe data access.
- **Type Safety & Validation**: TypeScript 5.9, Zod (`zod/v4`), and `drizzle-zod`.
- **API Code Generation**: Orval, generating client code from an OpenAPI specification.
- **Build System**: esbuild for CommonJS bundles.

### Authentication

The admin portal now requires login. The auth flow:
- `GET /api/auth/me` is called on startup; redirects to `/login` if no valid session.
- `POST /api/auth/login` with `{ email, password }` sets an `httponly` JWT cookie named `session` (7-day expiry).
- `POST /api/auth/logout` clears the cookie and returns to `/login`.
- Default credentials: email `admin@university-portal.local`, password `Bijay@12345` (overridden by `ADMIN_EMAIL`/`ADMIN_PASSWORD` env vars on the production server).
- Auth state managed by `src/context/auth.tsx` (`AuthProvider` + `useAuth` hook).
- All protected routes wrapped in `AuthGuard` in `App.tsx`.
- Logout button visible at the bottom of the sidebar when logged in.

### Core Features

- **Dashboard**: Provides an overview with statistics, courses by degree level, upcoming intakes, and recent changes.
- **University Management**: CRUD operations for universities, including viewing associated courses.
- **Course Management**: Comprehensive CRUD for courses, with detailed views covering intakes, fees, English requirements, academic requirements, and scholarships.
- **AI-Powered Web Scraper**: Extracts course data from university websites, utilizing AI for advanced data extraction and fallback mechanisms. Scraped data is staged for review.
- **Bulk Data Operations**: Supports bulk Excel uploads for importing course data and CSV downloads.
- **Data Import History**: Tracks all import jobs for auditing and review.
- **Scraping Job Management**: Includes functionalities to trigger, monitor status, and review/approve/reject scraped changes.
- **Repair Scrape**: A "back-fill only" pass for existing courses with blank key fields, ensuring data completeness without overwriting existing values.
- **Mode/Duration Extraction**: Robust extraction of study modes and course durations with AI fallback and rule-based parsing.
- **PDF Data Extraction**: Advanced parsing of PDF documents for fees and English requirements, including per-course matching in multi-row tables.
- **Per-host URL rewriting**: UNE appends `?international=true`; UOW appends `?students=international&year=<year>` before fetching each course page so the international-student fee, IELTS, intake, and campus data is visible.
- **UOW discovery**: BFS page budget raised to 80 (non-fast mode) and all 70 pagination pages pre-seeded so the full ~300 course catalogue is discovered.
- **Session → intake mapping (Pass 4)**: "Autumn Session" → March, "Spring Session" → July, "Summer Session" → November fallback for Australian universities (UOW-style).
- **PTE host blocklist**: UOW course pages don't publish PTE scores — a per-host blocklist suppresses false positives from Pattern-3 broad regex.
- **Location "Delivery method" fix**: Added `delivery\s*method` to `_TRAILING_KEYS` so that label is stripped from extracted location values.

### Data Model

The database schema includes tables for `universities`, `courses`, `intakes`, `fees`, `english_requirements`, `academic_requirements`, `scholarships`, `scraping_jobs`, `scraping_changes`, `scraped_courses` (staging), and `import_jobs`.

### Deployment Architecture

- **Production Server**: DigitalOcean droplet at `159.65.152.72`, Ubuntu 24.04.
- **Process Management**: systemd. Services: `uni-api-py.service` (FastAPI/uvicorn, port 8000) and `uni-celery.service` (Celery worker). Nginx proxies `/api` → `127.0.0.1:8000`.
- **Git repo on server**: `/root/University-and-Course-data`. Deploy = `git pull origin main` + `systemctl restart uni-api-py uni-celery`.
- **Database**: Local PostgreSQL. Database: `university_portal`, owner: `uniportal`. Access via `sudo -u postgres psql -d university_portal`. Schema changes via direct psql (alembic cannot be used on production — asyncpg fails to connect via TCP to `localhost` due to SSL hostname DNS issue).
- **CRITICAL — DB URL**: Must use `127.0.0.1` not `localhost` in the asyncpg connection string. asyncpg attempts SSL hostname verification using `getaddrinfo("localhost")` which fails on this server (`[Errno -3] Temporary failure in name resolution`). Using the IP literal bypasses the DNS lookup.  Hardcoded default in `backend-py/app/config.py` is already set to `127.0.0.1`.
- **alembic**: Do NOT run `alembic upgrade head` on production — it will fail with the same DNS error. Apply all schema changes via `sudo -u postgres psql -d university_portal -c "ALTER TABLE ..."` directly.
- **alembic_version table**: Contains fake version IDs (`001_initial` … `006_add_scrape_warnings`) inserted manually. The actual migration filenames are `001_add_rejection_reason`, `002_add_extraction_method`, etc. — these do NOT match. Ignore alembic version tracking on production entirely.
- **Environment Management**: DB credentials hardcoded in `app/config.py` default. No `.env` file needed on production.
- **journalctl**: The service does NOT log uvicorn application output to journalctl — only systemd lifecycle events appear. To see application errors, check `/tmp/dashboard_stats_error.log` (written by the try/except in dashboard.py) or run uvicorn in the foreground temporarily.

## External Dependencies

- **AI/ML**: Gemini API (`GEMINI_API_KEY`) for AI-powered web scraping and data extraction. Uses `gemini-2.5-flash`, `gemini-2.0-flash-001`, and `gemini-2.0-flash-lite-001` with auto-fallback.
- **Web Scraping**: Playwright for browser automation in the Python backend.
- **Message Queue**: Redis for Celery as a broker and result backend.
- **Web Server**: Nginx for serving the frontend and proxying API requests in production.
- **Database**: PostgreSQL.
- **Cloud Provider**: DigitalOcean for production hosting.