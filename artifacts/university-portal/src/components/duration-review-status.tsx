export type DurationReviewStatus = {
  status: "confirmed_unpublished";
  reason: string;
  sources: Array<{ url: string; snippet: string }>;
};

function safeSourceUrl(url: string): string | null {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch {
    return null;
  }
}

export function DurationReviewNotice({ status, id, onReport }: {
  status: DurationReviewStatus | null | undefined;
  id: number;
  onReport?: () => void;
}) {
  if (status?.status !== "confirmed_unpublished") return null;
  return (
    <div className="max-w-64 whitespace-normal text-left text-xs" data-testid={`status-duration-unpublished-${id}`}>
      <span className="font-semibold text-amber-800">Not published</span>
      <p className="text-muted-foreground">{status.reason}</p>
      {status.sources.map((source, index) => {
        const url = safeSourceUrl(source.url);
        return (
          <div key={`${source.url}-${index}`} className="mt-1">
            {url && (
              <a href={url} target="_blank" rel="noopener noreferrer"
                data-testid={`link-duration-source-${id}-${index}`}
                className="text-blue-700 underline break-all">
                Official source {index + 1}
              </a>
            )}
            {source.snippet && <p className="text-muted-foreground">{source.snippet}</p>}
          </div>
        );
      })}
      {onReport && (
        <button type="button" onClick={onReport} className="mt-1 text-blue-700 underline"
          data-testid={`button-report-duration-${id}`}>
          Report a new official URL / recheck
        </button>
      )}
    </div>
  );
}