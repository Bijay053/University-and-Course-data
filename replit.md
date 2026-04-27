# University Course, Fee, Intake & Requirement Management System

## Overview

This project provides a centralized administrative portal for universities to manage their course-related data. It enables comprehensive management of courses, fees, intakes, scholarships, and admission requirements. Key capabilities include AI-powered web scraping for data acquisition, bulk upload/download functionalities, and change detection mechanisms. The system aims to streamline data management for educational institutions, offering a robust solution for maintaining up-to-date and accurate course information.

## User Preferences

- Provide commands in the format `cd /root/University-and-Course-data && <command>`.
- Always provide commands in the specified format, especially for production deployment and verification.
- When schema changes are needed, explicitly provide the `pnpm --filter @workspace/db push --force` command before builds.
- For frontend-only changes, specify skipping the `api-server` build and `pm2 delete/start` commands.
- Provide verification commands to confirm commit deployment, new bundle serving, and correct PM2 environment variables.

## System Architecture

The system is built as a monorepo utilizing `pnpm workspaces`.

### Technology Stack

- **Frontend**: React with Vite, styled using Tailwind CSS and `shadcn/ui`. Data fetching is managed by TanStack React Query, and routing by `wouter`.
- **Backend (Local Dev)**: FastAPI (Python) serving on port 8080.
- **Backend (Production)**: Express 5 (Node.js) managed by PM2 and Nginx.
- **Database**: PostgreSQL with Drizzle ORM for type-safe data access.
- **Type Safety & Validation**: TypeScript 5.9, Zod (`zod/v4`), and `drizzle-zod`.
- **API Code Generation**: Orval, generating client code from an OpenAPI specification.
- **Build System**: esbuild for CommonJS bundles.

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

### Data Model

The database schema includes tables for `universities`, `courses`, `intakes`, `fees`, `english_requirements`, `academic_requirements`, `scholarships`, `scraping_jobs`, `scraping_changes`, `scraped_courses` (staging), and `import_jobs`.

### Deployment Architecture

- **Production Server**: DigitalOcean droplet running Ubuntu 24.04.
- **Process Management**: PM2 with `ecosystem.config.cjs` for Node.js API server.
- **Database**: Local PostgreSQL instance.
- **Environment Management**: `.env.backup` file on the server storing sensitive credentials.

## External Dependencies

- **AI/ML**: Gemini API (`GEMINI_API_KEY`) for AI-powered web scraping and data extraction. Uses `gemini-2.5-flash`, `gemini-2.0-flash-001`, and `gemini-2.0-flash-lite-001` with auto-fallback.
- **Web Scraping**: Playwright for browser automation in the Python backend.
- **Message Queue**: Redis for Celery as a broker and result backend.
- **Web Server**: Nginx for serving the frontend and proxying API requests in production.
- **Database**: PostgreSQL.
- **Cloud Provider**: DigitalOcean for production hosting.