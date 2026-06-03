/**
 * Guard: should a background poll completion trigger loadStagedCourses?
 *
 * Returns true  → caller should load staged courses for jobId.
 * Returns false → review panel is already open for a *different* job;
 *                 silently ignore the background completion.
 *
 * ── BEHAVIOUR CONTRACT ──────────────────────────────────────────────────────
 * Background polling must NEVER replace an already open review panel for a
 * different job. Only a deliberate user action passing force=true may switch
 * the review table to another job.
 *
 * This prevents silent review-table switching when:
 *   • another scrape job finishes in the background while the operator is
 *     reviewing staged courses from a previous job, or
 *   • another browser tab resumes an active job and its poll completion
 *     propagates to this tab via shared API state.
 *
 * Any call site that omits `force` (or passes force=false) is declaring
 * "I am a background event and must respect the operator's open panel."
 * Only explicit UI interactions (e.g. clicking a job row) should pass
 * force=true.
 * ────────────────────────────────────────────────────────────────────────────
 *
 * Extracted from the inline guard in pollJobStatus / handleReviewReady so it
 * can be unit-tested independently of the React component.
 */
export interface ReviewPanelState {
  showReview: boolean;
  reviewJobId: string | null;
}

/**
 * INVARIANT:
 * The review table always belongs to the most recently user-selected scrape
 * job, never the most recently completed scrape job.
 */
export function shouldLoadForBackgroundJob(
  jobId: string,
  panel: ReviewPanelState,
  force = false,
): boolean {
  if (force) return true;
  const reviewAlreadyOpen =
    panel.showReview &&
    panel.reviewJobId !== null &&
    panel.reviewJobId !== jobId;
  return !reviewAlreadyOpen;
}
