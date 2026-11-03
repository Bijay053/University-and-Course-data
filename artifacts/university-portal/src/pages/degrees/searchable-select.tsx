import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { Check, ChevronDown, Search } from "lucide-react";

export type SelectOption = { value: string; label: string };
type Props = {
  value: string; onChange: (value: string) => void; options: SelectOption[];
  anyLabel: string; label: string; testId: string;
  disabled?: boolean; searchPlaceholder?: string; emptyText?: string; className?: string;
};

/** Accessible single-select dropdown with a type-to-filter input. Renders inline (no portal) so it stays inside drawers. */
export function SearchableSelect({ value, onChange, options, anyLabel, label, testId, disabled, searchPlaceholder = "Type to filter", emptyText = "No matches", className = "" }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const uid = useId();
  const listId = `${uid}-list`;

  const items = useMemo(() => {
    const term = query.trim().toLocaleLowerCase();
    const matched = term ? options.filter((o) => o.label.toLocaleLowerCase().includes(term)) : options;
    return term ? matched : [{ value: "", label: anyLabel }, ...matched];
  }, [options, query, anyLabel]);
  const selected = options.find((o) => o.value === value);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (!rootRef.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);
  useEffect(() => { listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)?.scrollIntoView?.({ block: "nearest" }); }, [active]);

  const openMenu = () => {
    if (disabled) return;
    setQuery("");
    const idx = options.findIndex((o) => o.value === value);
    setActive(idx >= 0 ? idx + 1 : 0);
    setOpen(true);
  };
  const close = (refocus = true) => { setOpen(false); if (refocus) triggerRef.current?.focus(); };
  const choose = (v: string) => { onChange(v); close(); };

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((i) => Math.min(items.length - 1, i + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => Math.max(0, i - 1)); }
    else if (e.key === "Home") { e.preventDefault(); setActive(0); }
    else if (e.key === "End") { e.preventDefault(); setActive(items.length - 1); }
    else if (e.key === "Enter") { e.preventDefault(); const it = items[active]; if (it) choose(it.value); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    else if (e.key === "Tab") close(false);
  };

  return (
    <div ref={rootRef} className={`relative ${className}`}>
      <button ref={triggerRef} type="button" disabled={disabled} aria-haspopup="listbox" aria-expanded={open} aria-label={`${label}: ${selected?.label ?? anyLabel}`}
        data-testid={testId} data-value={value}
        onClick={() => (open ? close() : openMenu())}
        onKeyDown={(e) => { if (!open && (e.key === "ArrowDown" || e.key === "ArrowUp")) { e.preventDefault(); openMenu(); } }}
        className={`flex h-10 w-full items-center justify-between gap-2 rounded-lg border bg-white/90 px-3 text-left text-sm transition-colors disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary/30 ${open ? "border-[hsl(var(--sic-navy))]" : "border-[hsl(var(--sic-line))] hover:border-[hsl(var(--sic-navy))]/50"}`}>
        <span className={`min-w-0 truncate ${selected ? "font-medium text-[hsl(var(--sic-ink))]" : "text-[hsl(var(--sic-muted))]"}`}>{selected?.label ?? anyLabel}</span>
        <ChevronDown className={`h-4 w-4 shrink-0 text-[hsl(var(--sic-muted))] transition-transform ${open ? "rotate-180" : ""}`} aria-hidden />
      </button>
      {open && (
        <div className="relative mt-1 overflow-hidden rounded-lg border border-[hsl(var(--sic-line))] bg-white shadow-sm">
          <div className="sic-select-search flex items-center gap-2 border-b border-[hsl(var(--sic-line))] px-3 focus-within:bg-[hsl(var(--sic-mist))]">
            <Search className="h-3.5 w-3.5 shrink-0 text-[hsl(var(--sic-muted))]" aria-hidden />
            <input autoFocus role="combobox" aria-expanded aria-controls={listId} aria-autocomplete="list" aria-label={`Filter ${label}`}
              aria-activedescendant={items[active] ? `${uid}-opt-${active}` : undefined}
              value={query} onChange={(e) => { setQuery(e.target.value); setActive(0); }} onKeyDown={onKey}
              placeholder={searchPlaceholder} data-testid={`${testId}-filter`}
              className="h-9 min-w-0 flex-1 bg-transparent text-sm text-[hsl(var(--sic-ink))] outline-none placeholder:text-[hsl(var(--sic-muted))]" />
          </div>
          <ul ref={listRef} id={listId} role="listbox" aria-label={label} className="max-h-60 overflow-y-auto overscroll-contain py-1">
            {items.map((o, i) => {
              const isSel = o.value === value;
              return (
                <li key={o.value || "__any"} id={`${uid}-opt-${i}`} data-index={i} role="option" aria-selected={isSel}
                  onMouseDown={(e) => e.preventDefault()} onMouseEnter={() => setActive(i)} onClick={() => choose(o.value)}
                  className={`flex cursor-pointer items-center gap-2 px-3 py-2 text-sm ${i === active ? "bg-[hsl(var(--sic-mist))]" : ""} ${o.value ? "" : "text-[hsl(var(--sic-muted))]"} ${isSel ? "font-semibold text-[hsl(var(--sic-navy))]" : ""}`}>
                  <Check className={`h-3.5 w-3.5 shrink-0 text-primary ${isSel ? "" : "invisible"}`} aria-hidden />
                  <span className="min-w-0 break-words">{o.label}</span>
                </li>
              );
            })}
            {items.length === 0 && <li role="status" className="px-3 py-3 text-sm text-[hsl(var(--sic-muted))]">{emptyText}</li>}
          </ul>
        </div>
      )}
    </div>
  );
}
