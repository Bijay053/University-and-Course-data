import { X, Scale, Trash2 } from "lucide-react";
import { MAX_COMPARE, type ShortlistItem } from "./lib";

type Props = {
  open: boolean; items: ShortlistItem[]; compareIds: number[];
  onClose: () => void; onRemove: (id: number) => void; onClear: () => void;
  onToggleCompare: (id: number) => void; onCompare: () => void; onOpenCourse: (id: number) => void;
};

export function ShortlistDrawer({ open, items, compareIds, onClose, onRemove, onClear, onToggleCompare, onCompare, onOpenCourse }: Props) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-modal="true" aria-labelledby="shortlist-title" aria-describedby="shortlist-desc"
      onKeyDown={(e) => e.key === "Escape" && onClose()}>
      <button type="button" aria-label="Close shortlist" className="absolute inset-0 bg-[hsl(213_60%_12%/0.45)]" onClick={onClose} />
      <aside className="sic-rise relative h-full w-full max-w-md bg-[hsl(var(--sic-paper))] shadow-2xl flex flex-col" data-testid="drawer-shortlist">
        <header className="flex items-center justify-between p-5 border-b border-[hsl(var(--sic-line))]">
          <div>
            <h2 id="shortlist-title" className="sic-display text-xl">Your shortlist</h2>
            <p id="shortlist-desc" className="text-xs text-[hsl(var(--sic-muted))]">Saved on this device. Tick up to {MAX_COMPARE} to compare.</p>
          </div>
          <button type="button" autoFocus onClick={onClose} aria-label="Close" data-testid="button-close-shortlist" className="grid h-9 w-9 place-items-center rounded-full hover:bg-[hsl(var(--sic-teal-soft))]"><X className="h-4 w-4" aria-hidden /></button>
        </header>
        <div className="flex-1 overflow-y-auto p-5 space-y-2">
          {items.length === 0 ? (
            <div className="rounded-xl border border-dashed border-[hsl(var(--sic-line))] p-6 text-sm text-[hsl(var(--sic-muted))]">
              Nothing saved yet. Use the bookmark on any course card to keep it here.
            </div>
          ) : items.map((i) => {
            const checked = compareIds.includes(i.id);
            const disabled = !checked && compareIds.length >= MAX_COMPARE;
            return (
              <div key={i.id} className="flex items-start gap-3 rounded-xl border border-[hsl(var(--sic-line))] bg-white p-3" data-testid={`row-shortlist-${i.id}`}>
                <input type="checkbox" className="mt-1 h-4 w-4 accent-[hsl(182_48%_34%)]" checked={checked} disabled={disabled}
                  aria-label={`Compare ${i.course_name}`} onChange={() => onToggleCompare(i.id)} data-testid={`checkbox-compare-${i.id}`} />
                <button type="button" className="flex-1 text-left" onClick={() => onOpenCourse(i.id)}>
                  <span className="block text-sm font-semibold">{i.course_name}</span>
                  <span className="block text-xs text-[hsl(var(--sic-muted))]">{i.university || "University not published"}</span>
                </button>
                <button type="button" onClick={() => onRemove(i.id)} aria-label={`Remove ${i.course_name}`} data-testid={`button-remove-shortlist-${i.id}`}
                  className="grid h-8 w-8 place-items-center rounded-full text-[hsl(var(--sic-muted))] hover:bg-[hsl(var(--sic-mist))]"><Trash2 className="h-4 w-4" aria-hidden /></button>
              </div>
            );
          })}
        </div>
        <footer className="p-5 border-t border-[hsl(var(--sic-line))] flex gap-2">
          <button type="button" onClick={onClear} disabled={!items.length} data-testid="button-clear-shortlist"
            className="h-10 px-4 rounded-lg border border-[hsl(var(--sic-line))] text-sm disabled:opacity-50">Clear</button>
          <button type="button" onClick={onCompare} disabled={compareIds.length < 2} data-testid="button-compare"
            className="flex-1 h-10 inline-flex items-center justify-center gap-2 rounded-lg bg-[hsl(var(--sic-navy))] text-white text-sm font-semibold disabled:opacity-50">
            <Scale className="h-4 w-4" aria-hidden /> Compare {compareIds.length} of {MAX_COMPARE}
          </button>
        </footer>
      </aside>
    </div>
  );
}
