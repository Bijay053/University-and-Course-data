import type { PublishedFeeSchedule } from "@workspace/api-client-react";
import { safeSourceUrl } from "./assistant-safe-url";

export function PublishedIntakeFees({ schedules, reviewRequired = false }: {
  schedules: PublishedFeeSchedule[]; reviewRequired?: boolean;
}) {
  if (reviewRequired) return <p role="alert">Intake-linked tuition requires review.</p>;
  return <ul aria-label="Intake-specific tuition fees" className="space-y-2 whitespace-normal text-sm">
    {schedules.map(s => <li key={s.id}>
      <strong>{s.intakes.join(", ")} {s.entryYear}</strong>: {s.currency} {s.amount.toLocaleString("en-GB")} / {s.basis}
      <span className="block text-xs">Fee year {s.feeYear}</span>
      {s.sourceUrls.map((url, i) => {
        const safe = safeSourceUrl(url);
        return safe ? <a key={url} href={safe} target="_blank" rel="noopener noreferrer nofollow"
          className="mr-2 text-xs underline">Official source {i + 1}</a> : null;
      })}
    </li>)}
  </ul>;
}
