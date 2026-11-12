/** Read-only polling that discovers work started in another session. */
export function startVisiblePolling(
  refresh: (signal: AbortSignal) => Promise<void>,
  intervalMs: number,
  onError: (error: unknown) => void,
  immediate = false,
): () => void {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let inFlight = false;
  const schedule = () => {
    if (timer) clearTimeout(timer);
    if (!controller.signal.aborted && !document.hidden) {
      timer = setTimeout(() => { void tick(); }, intervalMs);
    }
  };
  const tick = async () => {
    if (controller.signal.aborted || inFlight || document.hidden) return;
    if (timer) clearTimeout(timer);
    inFlight = true;
    try {
      await refresh(controller.signal);
    } catch (error) {
      if (!controller.signal.aborted) onError(error);
    } finally {
      inFlight = false;
      schedule();
    }
  };
  const onFocus = () => { void tick(); };
  const onVisibility = () => {
    if (document.hidden) {
      if (timer) clearTimeout(timer);
    } else {
      void tick();
    }
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onVisibility);
  if (immediate) void tick();
  else schedule();
  return () => {
    controller.abort();
    if (timer) clearTimeout(timer);
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onVisibility);
  };
}

export function preferFreshRepair<T extends {
  jobId: string; sourceJobId: string | null; status: string;
  processed: number; createdAt?: string | null;
}>(current: T | null, incoming: T): T {
  if (!current || current.sourceJobId !== incoming.sourceJobId) return incoming;
  if (current.jobId === incoming.jobId) {
    if (current.processed > incoming.processed) return current;
    const active = (status: string) => ["queued", "running"].includes(status);
    if (!active(current.status) && active(incoming.status)
      && current.processed >= incoming.processed) return current;
  } else {
    const currentTime = Date.parse(current.createdAt ?? "");
    const incomingTime = Date.parse(incoming.createdAt ?? "");
    if (Number.isFinite(currentTime) && (!Number.isFinite(incomingTime) || currentTime > incomingTime)) {
      return current;
    }
  }
  return incoming;
}
