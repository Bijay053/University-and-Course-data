import { useMemo, useState } from "react";
import { Check, ChevronDown, X, Search } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export type MultiSelectOption = { value: string; label: string; count?: number | null };

interface MultiSelectProps {
  options: MultiSelectOption[];
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  searchPlaceholder?: string;
  emptyText?: string;
  maxBadgeCount?: number;
  triggerClassName?: string;
}

export function MultiSelect({
  options, value, onChange,
  placeholder = "Select...",
  searchPlaceholder = "Search...",
  emptyText = "No results.",
  maxBadgeCount = 2,
  triggerClassName,
}: MultiSelectProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter((o) => o.label.toLowerCase().includes(q));
  }, [options, query]);

  const toggle = (v: string) => {
    onChange(value.includes(v) ? value.filter((x) => x !== v) : [...value, v]);
  };

  const labelMap = useMemo(() => {
    const m = new Map<string, string>();
    for (const o of options) m.set(o.value, o.label);
    return m;
  }, [options]);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          role="combobox"
          aria-expanded={open}
          className={cn(
            "h-9 w-full justify-between font-normal text-left",
            value.length === 0 && "text-gray-400",
            triggerClassName,
          )}
        >
          <div className="flex flex-wrap gap-1 items-center min-w-0 flex-1">
            {value.length === 0 ? (
              <span className="truncate">{placeholder}</span>
            ) : value.length <= maxBadgeCount ? (
              value.map((v) => (
                <Badge key={v} variant="secondary" className="text-xs gap-1 max-w-[140px]">
                  <span className="truncate">{labelMap.get(v) ?? v}</span>
                  <span
                    role="button"
                    tabIndex={0}
                    aria-label={`Remove ${labelMap.get(v) ?? v}`}
                    onClick={(e) => { e.stopPropagation(); toggle(v); }}
                    onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); toggle(v); } }}
                    className="hover:text-red-600 cursor-pointer inline-flex"
                  >
                    <X className="w-3 h-3" />
                  </span>
                </Badge>
              ))
            ) : (
              <span className="text-sm text-gray-700">{value.length} selected</span>
            )}
          </div>
          <ChevronDown className="h-4 w-4 opacity-50 ml-2 flex-shrink-0" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="p-0 w-[280px]" align="start">
        <div className="p-2 border-b">
          <div className="relative">
            <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-gray-400" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={searchPlaceholder}
              className="h-8 pl-7 text-sm"
            />
          </div>
        </div>
        <div className="max-h-64 overflow-y-auto py-1">
          {filtered.length === 0 ? (
            <p className="px-3 py-4 text-xs text-gray-500 text-center">{emptyText}</p>
          ) : (
            filtered.map((o) => {
              const selected = value.includes(o.value);
              return (
                <button
                  key={o.value}
                  type="button"
                  onClick={() => toggle(o.value)}
                  className={cn(
                    "flex items-center w-full px-3 py-1.5 text-sm hover:bg-gray-50 text-left gap-2",
                    selected && "bg-blue-50",
                  )}
                >
                  <div className={cn(
                    "w-4 h-4 rounded border flex items-center justify-center flex-shrink-0",
                    selected ? "bg-blue-600 border-blue-600" : "border-gray-300",
                  )}>
                    {selected && <Check className="w-3 h-3 text-white" />}
                  </div>
                  <span className="flex-1 truncate">{o.label}</span>
                  {o.count != null && <span className="text-xs text-gray-400">{o.count}</span>}
                </button>
              );
            })
          )}
        </div>
        {value.length > 0 && (
          <div className="border-t p-2 flex justify-between items-center">
            <span className="text-xs text-gray-500">{value.length} selected</span>
            <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => onChange([])}>
              Clear
            </Button>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
