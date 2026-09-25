import { feeVariantAuthority, type FeeVariantCarrier } from "@/components/published-fee-variants";

type Row = FeeVariantCarrier & { id: number; status?: string };
type Result = { id: number; status: string; courseIds: number[]; reason?: string };
const object = (v: unknown): Record<string, unknown> | undefined =>
  v !== null && typeof v === "object" ? v as Record<string, unknown> : undefined;

export function needsCampusPreparation(row: Row) {
  if (row.status && !["pending", "review_ready"].includes(row.status)) return false;
  const metadata = object(row.extractionMethod) ?? object(row.extraction_method);
  const scope = object(metadata?.campus_fee_scope);
  if (scope) return Array.isArray(scope.locations) && scope.locations.length > 1;
  return !!feeVariantAuthority(row);
}

/** One explicit load cycle; never recursively retry uncertain source evidence. */
export async function prepareCampusReview<T extends Row>(
  rows: T[], reload: () => Promise<T[]>,
  progress: (done: number, total: number) => void,
  signal?: AbortSignal,
) {
  const candidates = rows.filter(needsCampusPreparation);
  const results: Result[] = [];
  let next = 0, done = 0;
  let permissionDenied = false;
  progress(0, candidates.length);
  await Promise.all(Array.from({ length: Math.min(2, candidates.length) }, async () => {
    while (next < candidates.length) {
      if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
      const row = candidates[next++];
      try {
        if (permissionDenied) throw new Error("You do not have permission to prepare campus courses.");
        const response = await fetch("/api/scrape/staged/prepare-campus-courses", {
          signal,
          method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids: [row.id] }),
        });
        if (response.status === 403 || response.status === 401) permissionDenied = true;
        if (!response.ok) throw new Error(permissionDenied ? "You do not have permission to prepare campus courses." : `Campus verification failed (${response.status}). Refresh to retry.`);
        const body = await response.json();
        const result = body.results?.find((r: Result) => r.id === row.id);
        if (!result || !Array.isArray(result.courseIds)) throw new Error("Campus verification was not confirmed. Refresh to retry.");
        results.push(result);
      } catch (error) {
        results.push({ id: row.id, status: "needs_review", courseIds: [row.id],
          reason: error instanceof Error ? error.message : "Campus verification failed. Refresh to retry." });
      } finally {
        progress(++done, candidates.length);
      }
    }
  }));
  if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
  // Reload only once, after the bounded batch; unchanged/uncertain rows don't loop.
  const refreshed = candidates.length ? await reload() : rows;
  return { rows: refreshed, results, issues: results.filter(r => r.status === "needs_review") };
}