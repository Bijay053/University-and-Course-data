# University Course, Fee, Intake & Requirement Management System

## Overview

A centralized admin portal for managing university course data including courses, fees, intakes, scholarships, and admission requirements. Supports web scraping, bulk upload/download, and change detection.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **Frontend**: React + Vite + Tailwind CSS + shadcn/ui + TanStack React Query + wouter
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Key Artifacts

- **`artifacts/university-portal`** — React + Vite frontend at `/`
- **`artifacts/api-server`** — Express 5 API server at `/api`

## Pages

- `/` — Dashboard with stats, courses by degree level, upcoming intakes, recent changes
- `/universities` — Searchable university list with add/view actions
- `/universities/:id` — University detail with courses
- `/courses` — Searchable/filterable course list
- `/courses/:id` — Course detail with tabs: Overview, Intakes, Fees, English Requirements, Academic Requirements, Scholarships
- `/courses/new` — Create new course
- `/scraping` — Scraping jobs and change detection (approve/reject)
- `/bulk` — Bulk CSV upload and download

## Database Schema

Tables: `universities`, `courses`, `intakes`, `fees`, `english_requirements`, `academic_requirements`, `scholarships`, `scraping_jobs`, `scraping_changes`

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally
- `pnpm --filter @workspace/university-portal run dev` — run frontend locally

## API Routes

All routes served at `/api/...`:
- `GET/POST /universities` — list/create universities
- `GET/PATCH/DELETE /universities/:id` — university CRUD
- `GET/POST /courses` — list/create courses
- `GET/PATCH/DELETE /courses/:id` — course CRUD
- `GET/POST /courses/:courseId/intakes` — intake management
- `PATCH/DELETE /intakes/:id` — intake update/delete
- `GET/POST /courses/:courseId/fees` — fee management
- `PATCH/DELETE /fees/:id` — fee update/delete
- `GET/POST /courses/:courseId/english-requirements` — English requirement management
- `PATCH/DELETE /english-requirements/:id`
- `GET/POST /courses/:courseId/academic-requirements` — academic requirement management
- `PATCH/DELETE /academic-requirements/:id`
- `GET/POST /courses/:courseId/scholarships` — scholarship management
- `PATCH/DELETE /scholarships/:id`
- `GET/POST /scraping/jobs` — scraping job management
- `POST /scraping/jobs/:id/run` — trigger scraping job
- `GET /scraping/changes` — list detected changes
- `POST /scraping/changes/:id/approve|reject` — review changes
- `GET /dashboard/stats|recent-changes|courses-by-level|upcoming-intakes` — dashboard data
- `GET /bulk/courses/download` — CSV download
- `POST /bulk/courses/upload` — CSV upload
