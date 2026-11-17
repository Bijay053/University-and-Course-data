import React from "react";

type Props = {
  pendingCount: number | null;
  originallyStaged?: number | null;
  refreshedAt?: number | null;
};

/** Current persisted review state, never a rewritten extraction-history counter. */
export function CurrentReviewSummary({ pendingCount, originallyStaged, refreshedAt }: Props) {
  if (pendingCount === null) {
    return <p className="text-xs text-muted-foreground">
      Current pending count has not loaded. Original scrape totals below are historical.
    </p>;
  }
  const difference = typeof originallyStaged === "number" ? pendingCount - originallyStaged : 0;
  return <div className="rounded-md border border-green-200 bg-green-50 px-3 py-2 text-xs text-green-950"
    aria-live="polite" data-testid="current-review-summary">
    <strong>Current review: {pendingCount} pending {pendingCount === 1 ? "entry" : "entries"}.</strong>
    {difference !== 0 && <span> {Math.abs(difference)} {difference < 0 ? "fewer" : "more"} than
      {" "}the {originallyStaged} originally staged; saved review changes are included.</span>}
    <p className="mt-1">Current counts update after saved changes. Original scrape logs are historical and are not rewritten.</p>
    {refreshedAt != null && <p className="mt-1">Last reloaded:{" "}
      <time dateTime={new Date(refreshedAt).toISOString()}>{new Date(refreshedAt).toLocaleTimeString()}</time>
    </p>}
  </div>;
}
