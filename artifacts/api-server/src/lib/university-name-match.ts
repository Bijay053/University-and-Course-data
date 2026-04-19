import { db, universitiesTable } from "@workspace/db";
import { sql } from "drizzle-orm";

/** Match existing university rows regardless of name casing (e.g. "asa" vs "ASA"). */
export async function findUniversityByNameCaseInsensitive(name: string) {
  const trimmed = name.trim();
  if (!trimmed) return undefined;
  const rows = await db
    .select()
    .from(universitiesTable)
    .where(sql`lower(${universitiesTable.name}) = lower(${trimmed})`)
    .limit(1);
  return rows[0];
}

/** Append setup hints for common local Postgres / Drizzle failures shown in the UI. */
export function formatDatabaseSetupHint(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err);
  if (
    /role .* does not exist|password authentication failed|ECONNREFUSED|connect ECONNREFUSED|getaddrinfo ENOTFOUND|database .* does not exist|relation .* does not exist|Failed query:/i.test(
      msg,
    )
  ) {
    return (
      `${msg} — Check DATABASE_URL in .env (see .env.example): PostgreSQL must be running, the DB user must exist ` +
      `(on macOS the default superuser is often your macOS username, not "postgres"), and migrations must be applied.`
    );
  }
  return msg;
}
