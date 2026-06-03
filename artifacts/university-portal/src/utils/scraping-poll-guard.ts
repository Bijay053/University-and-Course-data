/**
 * Guard: should a background poll completion trigger loadStagedCourses?
 *
 * Returns true  → caller should load staged courses for jobId.
 * Returns false → review panel is already open for a *different* job;
 *                 silently ignore the background completion.
 *
 * Extracted from the inline guard in pollJobStatus / handleReviewReady so it
 * can be unit-tested independently of the React component.
 */
export interface ReviewPanelState {
  showReview: boolean;
  reviewJobId: string | null;
}

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
