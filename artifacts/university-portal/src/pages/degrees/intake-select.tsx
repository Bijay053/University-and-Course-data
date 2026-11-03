import { useEffect, useId, useRef, useState } from "react";
import { ChevronDown, Search } from "lucide-react";

type Props = { options: string[]; value: string[]; onChange: (value: string[]) => void };

/** In-flow checkbox dropdown so accordion and mobile drawer boundaries cannot clip it. */
export function IntakeSelect({ options, value, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  const choices = Array.from(new Set([...options, ...value]));
  const visible = choices.filter((month) => month.toLowerCase().includes(query.trim().toLowerCase()));

  useEffect(() => {
    if (!open) return;
    const outside = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, [open]);

  return (
    <div ref={root} onKeyDown={(event) => {
      if (open && event.key === "Escape") {
        event.preventDefault(); event.stopPropagation();
        setOpen(false); trigger.current?.focus();
      }
    }}>
      <button ref={trigger} type="button" aria-expanded={open} aria-controls={id}
        data-testid="select-intakes" onClick={() => { setQuery(""); setOpen(!open); }}
        className="flex min-h-10 w-full items-center justify-between gap-2 rounded-lg border border-[hsl(var(--sic-line))] bg-white px-3 py-2 text-left text-sm text-[hsl(var(--sic-ink))]">
        <span className="min-w-0 break-words">{value.length ? value.join(", ") : "Any intake"}</span>
        <ChevronDown className={`h-4 w-4 shrink-0 text-[hsl(var(--sic-muted))] ${open ? "rotate-180" : ""}`} aria-hidden />
      </button>
      {open && (
        <div id={id} className="mt-1 overflow-hidden rounded-lg border border-[hsl(var(--sic-line))] bg-white shadow-sm">
          <div className="sic-select-search flex items-center gap-2 border-b border-[hsl(var(--sic-line))] px-3">
            <Search className="h-3.5 w-3.5 shrink-0 text-[hsl(var(--sic-muted))]" aria-hidden />
            <input autoFocus value={query} onChange={(event) => setQuery(event.target.value)}
              aria-label="Search intake months" placeholder="Search intakes"
              className="h-9 min-w-0 w-full bg-transparent text-sm text-[hsl(var(--sic-ink))] outline-none" />
          </div>
          <div role="group" aria-label="Intake months" className="max-h-56 overflow-y-auto overscroll-contain p-1">
            {visible.map((month) => (
              <label key={month} className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-2 text-sm text-[hsl(var(--sic-ink))] hover:bg-[hsl(var(--sic-mist))]">
                <input type="checkbox" checked={value.includes(month)} data-testid={`toggle-intake-${month}`}
                  onChange={() => onChange(value.includes(month) ? value.filter((v) => v !== month) : [...value, month])}
                  className="h-4 w-4 shrink-0 accent-[hsl(var(--primary))]" />
                {month}
              </label>
            ))}
            {!visible.length && <p role="status" className="p-2 text-sm text-[hsl(var(--sic-muted))]">No intakes found.</p>}
          </div>
          <div className="flex items-center justify-between border-t border-[hsl(var(--sic-line))] px-3 py-2 text-xs">
            <span aria-live="polite" className="text-[hsl(var(--sic-muted))]">{value.length} selected</span>
            <button type="button" disabled={!value.length} onClick={() => onChange([])} className="font-semibold text-primary disabled:opacity-40">Clear intakes</button>
            <button type="button" onClick={() => { setOpen(false); trigger.current?.focus(); }} className="font-semibold text-[hsl(var(--sic-ink))]">Done</button>
          </div>
        </div>
      )}
    </div>
  );
}
