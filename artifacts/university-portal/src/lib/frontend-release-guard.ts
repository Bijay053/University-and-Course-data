const HASHED_ENTRY = /\/assets\/index-[A-Za-z0-9_-]+\.js(?:$|[?#])/;

function absoluteUrl(value: string, baseUrl: string): string {
  return new URL(value, baseUrl).href;
}

export function findHashedEntryScript(
  root: ParentNode,
  baseUrl: string,
): string | null {
  for (const script of root.querySelectorAll<HTMLScriptElement>("script[src]")) {
    const source = absoluteUrl(script.getAttribute("src") ?? "", baseUrl);
    if (HASHED_ENTRY.test(source)) return source;
  }
  return null;
}

export async function checkForFrontendRelease({
  currentEntry,
  indexUrl,
  fetcher,
  reload,
}: {
  currentEntry: string;
  indexUrl: string;
  fetcher: typeof fetch;
  reload: () => void;
}): Promise<boolean> {
  const requestUrl = new URL(indexUrl);
  requestUrl.searchParams.set("__frontend_release_check", String(Date.now()));
  const response = await fetcher(requestUrl.href, {
    cache: "no-store",
    credentials: "same-origin",
    headers: { "Cache-Control": "no-cache" },
  });
  if (!response.ok) return false;

  const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
  const latestEntry = findHashedEntryScript(parsed, indexUrl);
  if (!latestEntry || latestEntry === currentEntry) return false;

  reload();
  return true;
}

export function startFrontendReleaseGuard(): () => void {
  const currentEntry = findHashedEntryScript(document, window.location.href);
  if (!currentEntry) return () => undefined;

  const entryUrl = new URL(currentEntry);
  const assetsIndex = entryUrl.pathname.lastIndexOf("/assets/");
  if (assetsIndex < 0) return () => undefined;
  const indexUrl = new URL(`${entryUrl.pathname.slice(0, assetsIndex + 1)}`, entryUrl.origin).href;

  let checking = false;
  const check = async () => {
    if (checking) return;
    checking = true;
    try {
      await checkForFrontendRelease({
        currentEntry,
        indexUrl,
        fetcher: window.fetch.bind(window),
        reload: () => window.location.reload(),
      });
    } catch {
      // A transient network failure must not interrupt the running application.
    } finally {
      checking = false;
    }
  };

  const interval = window.setInterval(() => void check(), 60_000);
  const onFocus = () => void check();
  const onVisibilityChange = () => {
    if (document.visibilityState === "visible") void check();
  };
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onVisibilityChange);

  return () => {
    window.clearInterval(interval);
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onVisibilityChange);
  };
}